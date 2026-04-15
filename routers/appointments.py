import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_
from slowapi import Limiter
from slowapi.util import get_remote_address
from dependencies import get_db, get_clinic_id, get_user_id, get_roles
from models import Appointment, DentistCalendarConfig, AppointmentStatus, AppointmentAuditLog
from schemas import AppointmentCreate, AppointmentResponse, AppointmentStatusUpdate
from utils.google_calendar import get_calendar_service, get_free_busy, create_google_event
from utils.crypto import decrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

@router.get("/availability/dentist/{dentist_id}")
@limiter.limit("30/minute")
async def get_dentist_availability(
    request: Request,
    dentist_id: int,
    start: datetime,
    end: datetime,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id)
):
    """Calcula los huecos libres de un odontólogo uniendo Google + Citas locales."""
    logger.info(f"Fetching availability for dentist_id: {dentist_id} (clinic_id: {clinic_id}) from {start} to {end} by user: {user_id}")
    
    # 1. Obtener configuración de Google del odontólogo
    config = db.query(DentistCalendarConfig).filter(
        and_(
            DentistCalendarConfig.dentist_user_id == dentist_id,
            DentistCalendarConfig.clinic_id == clinic_id
        )
    ).first()

    if not config or not config.google_refresh_token:
        logger.warning(f"Availability request failed: Dentist {dentist_id} has not connected Google Calendar (clinic_id: {clinic_id})")
        raise HTTPException(status_code=400, detail="Dentist has not connected Google Calendar")

    try:
        # 2. Consultar Google Free/Busy
        access_token = decrypt_token(config.google_access_token)
        refresh_token = decrypt_token(config.google_refresh_token)
        
        service = get_calendar_service(access_token, refresh_token, config.token_expiry)
        google_busy = get_free_busy(service, "primary", start, end)

        # 3. Consultar Citas locales
        local_appointments = db.query(Appointment).filter(
            and_(
                Appointment.dentist_user_id == dentist_id,
                Appointment.clinic_id == clinic_id,
                Appointment.status.in_([AppointmentStatus.REQUESTED, AppointmentStatus.APPROVED]),
                Appointment.start_time_utc >= start,
                Appointment.end_time_utc <= end
            )
        ).all()

        local_busy = [
            {"start": apt.start_time_utc.isoformat(), "end": apt.end_time_utc.isoformat()}
            for apt in local_appointments
        ]

        logger.info(f"Availability successfully fetched for dentist_id: {dentist_id}. Found {len(google_busy)} Google blocks and {len(local_busy)} local appointments.")
        return {
            "dentist_id": dentist_id,
            "busy_slots": google_busy + local_busy
        }
    except Exception as e:
        logger.error(f"Error fetching availability for dentist {dentist_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch availability")

@router.post("/", response_model=AppointmentResponse)
@limiter.limit("10/minute")
async def request_appointment(
    request: Request,
    apt_data: AppointmentCreate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    patient_id: int = Depends(get_user_id)
):
    """Crea una solicitud de turno (Estado: REQUESTED)."""
    logger.info(f"Requesting new appointment for patient_id: {patient_id} with dentist_id: {apt_data.dentist_user_id} (clinic_id: {clinic_id})")
    
    new_apt = Appointment(
        clinic_id=clinic_id,
        patient_user_id=patient_id,
        dentist_user_id=apt_data.dentist_user_id,
        start_time_utc=apt_data.start_time_utc,
        end_time_utc=apt_data.end_time_utc,
        patient_timezone=apt_data.patient_timezone,
        reason=apt_data.reason,
        status=AppointmentStatus.REQUESTED
    )
    
    db.add(new_apt)
    db.commit()
    db.refresh(new_apt)
    
    logger.info(f"Appointment request created successfully with id: {new_apt.appointment_id} for patient: {patient_id}")
    return new_apt

@router.patch("/{appointment_id}/status", response_model=AppointmentResponse)
@limiter.limit("20/minute")
async def update_appointment_status(
    request: Request,
    appointment_id: int,
    status_update: AppointmentStatusUpdate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles)
):
    """Aprueba, rechaza o cancela un turno con control de roles.

    Reglas de acceso:
    - APPROVED / REJECTED / COMPLETED: solo el dentista asignado o un ADMIN.
    - CANCELLED: el paciente dueño del turno, el dentista asignado, o un ADMIN.
    """
    logger.info(f"Updating appointment {appointment_id} to {status_update.status} by user {user_id} roles={roles} (clinic {clinic_id})")

    apt = db.query(Appointment).filter(
        and_(
            Appointment.appointment_id == appointment_id,
            Appointment.clinic_id == clinic_id
        )
    ).first()

    if not apt:
        logger.warning(f"Appointment {appointment_id} not found for clinic {clinic_id}")
        raise HTTPException(status_code=404, detail="Appointment not found")

    # --- Validación de transición de estado ---
    ALLOWED_TRANSITIONS = {
        AppointmentStatus.REQUESTED: {AppointmentStatus.APPROVED, AppointmentStatus.REJECTED, AppointmentStatus.CANCELLED},
        AppointmentStatus.APPROVED:  {AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED},
        AppointmentStatus.REJECTED:  set(),
        AppointmentStatus.COMPLETED: set(),
        AppointmentStatus.CANCELLED: set(),
    }
    if status_update.status not in ALLOWED_TRANSITIONS.get(apt.status, set()):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot transition from {apt.status.value} to {status_update.status.value}"
        )

    # --- RBAC ---
    is_admin    = "ADMIN" in roles
    is_dentist  = "DENTIST" in roles
    is_patient  = "PATIENT" in roles

    if status_update.status in (
        AppointmentStatus.APPROVED,
        AppointmentStatus.REJECTED,
        AppointmentStatus.COMPLETED
    ):
        # Solo el dentista asignado a este turno, o un admin
        if not is_admin and not (is_dentist and apt.dentist_user_id == user_id):
            logger.warning(
                f"Forbidden: user {user_id} (roles={roles}) tried to {status_update.status.value} "
                f"appointment {appointment_id} assigned to dentist {apt.dentist_user_id}"
            )
            raise HTTPException(
                status_code=403,
                detail="Only the assigned dentist or an admin can approve, reject or complete appointments"
            )

    elif status_update.status == AppointmentStatus.CANCELLED:
        # El paciente puede cancelar su propio turno; el dentista asignado también; admin siempre
        is_own_patient = is_patient and apt.patient_user_id == user_id
        is_own_dentist = is_dentist and apt.dentist_user_id == user_id
        if not is_admin and not is_own_patient and not is_own_dentist:
            logger.warning(
                f"Forbidden: user {user_id} (roles={roles}) tried to cancel appointment {appointment_id} "
                f"(patient={apt.patient_user_id}, dentist={apt.dentist_user_id})"
            )
            raise HTTPException(
                status_code=403,
                detail="You can only cancel your own appointments"
            )
    # --- fin RBAC ---

    previous_status = apt.status.value
    apt.status = status_update.status
    
    if status_update.status == AppointmentStatus.APPROVED:
        logger.info(f"Appointment {appointment_id} APPROVED. Initiating Google Calendar sync...")
        config = db.query(DentistCalendarConfig).filter(
            and_(
                DentistCalendarConfig.dentist_user_id == apt.dentist_user_id,
                DentistCalendarConfig.clinic_id == clinic_id
            )
        ).first()

        if config and config.google_refresh_token:
            try:
                access_token = decrypt_token(config.google_access_token)
                refresh_token = decrypt_token(config.google_refresh_token)
                service = get_calendar_service(access_token, refresh_token, config.token_expiry)
                
                event_id = create_google_event(
                    service, 
                    "primary", 
                    apt.start_time_utc, 
                    apt.end_time_utc,
                    f"Cita Dental: {apt.reason or 'Consulta'}",
                    f"Cita aprobada desde el portal odontológico. ID: {apt.appointment_id}"
                )
                apt.google_event_id = event_id
                logger.info(f"Google Calendar sync successful for appointment {appointment_id}. Event ID: {event_id}")
            except Exception as e:
                logger.error(f"Failed to sync appointment {appointment_id} with Google: {e}")

    # Guardar auditoría
    audit = AppointmentAuditLog(
        appointment_id=apt.appointment_id,
        changed_by_user_id=user_id,
        previous_status=previous_status,
        new_status=status_update.status.value,
        change_reason=status_update.change_reason
    )
    db.add(audit)
    db.commit()
    db.refresh(apt)
    
    logger.info(f"Appointment status for id: {appointment_id} updated successfully from {previous_status} to {apt.status.value}")
    return apt
