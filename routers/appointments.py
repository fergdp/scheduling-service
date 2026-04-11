import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_
from ..dependencies import get_db, get_clinic_id, get_user_id
from ..models import Appointment, DentistCalendarConfig, AppointmentStatus, AppointmentAuditLog
from ..schemas import AppointmentCreate, AppointmentResponse, AppointmentStatusUpdate
from ..utils.google_calendar import get_calendar_service, get_free_busy, create_google_event
from ..utils.crypto import decrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/availability/dentist/{dentist_id}")
async def get_dentist_availability(
    dentist_id: int,
    start: datetime,
    end: datetime,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id)
):
    """Calcula los huecos libres de un odontólogo uniendo Google + Citas locales."""
    # 1. Obtener configuración de Google del odontólogo
    config = db.query(DentistCalendarConfig).filter(
        and_(
            DentistCalendarConfig.dentist_user_id == dentist_id,
            DentistCalendarConfig.clinic_id == clinic_id
        )
    ).first()

    if not config or not config.google_refresh_token:
        raise HTTPException(status_code=400, detail="Dentist has not connected Google Calendar")

    try:
        # 2. Consultar Google Free/Busy
        # Desencriptamos los tokens
        access_token = decrypt_token(config.google_access_token)
        refresh_token = decrypt_token(config.google_refresh_token)
        
        service = get_calendar_service(access_token, refresh_token, config.token_expiry)
        google_busy = get_free_busy(service, "primary", start, end)

        # 3. Consultar Citas locales (Incluso las pendientes de aprobación)
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

        # Devolvemos la unión de bloques ocupados
        return {
            "dentist_id": dentist_id,
            "busy_slots": google_busy + local_busy
        }
    except Exception as e:
        logger.error(f"Error fetching availability: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch availability")

@router.post("/", response_model=AppointmentResponse)
async def request_appointment(
    apt_data: AppointmentCreate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    patient_id: int = Depends(get_user_id)
):
    """Crea una solicitud de turno (Estado: REQUESTED)."""
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
    return new_apt

@router.patch("/{appointment_id}/status", response_model=AppointmentResponse)
async def update_appointment_status(
    appointment_id: int,
    status_update: AppointmentStatusUpdate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    dentist_id: int = Depends(get_user_id)
):
    """Aprueba o rechaza un turno. Si se aprueba, se sincroniza con Google."""
    apt = db.query(Appointment).filter(
        and_(
            Appointment.appointment_id == appointment_id,
            Appointment.clinic_id == clinic_id
        )
    ).first()

    if not apt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    # Auditoría del cambio
    previous_status = apt.status.value
    apt.status = status_update.status
    
    # Si se aprueba, impacto en Google Calendar
    if status_update.status == AppointmentStatus.APPROVED:
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
                
                # Crear evento en Google
                event_id = create_google_event(
                    service, 
                    "primary", 
                    apt.start_time_utc, 
                    apt.end_time_utc,
                    f"Cita Dental: {apt.reason or 'Consulta'}",
                    f"Cita aprobada desde el portal odontológico. ID: {apt.appointment_id}"
                )
                apt.google_event_id = event_id
            except Exception as e:
                logger.error(f"Failed to sync with Google: {e}")
                # Podríamos decidir si fallar o simplemente avisar que no se sincronizó.
                # Aquí dejaremos que la cita se apruebe localmente de todos modos.

    # Guardar auditoría
    audit = AppointmentAuditLog(
        appointment_id=apt.appointment_id,
        changed_by_user_id=dentist_id,
        previous_status=previous_status,
        new_status=status_update.status.value,
        change_reason=status_update.change_reason
    )
    db.add(audit)
    db.commit()
    db.refresh(apt)
    return apt
