import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_
from slowapi import Limiter
from slowapi.util import get_remote_address
from dependencies import get_db, get_clinic_id, get_user_id, get_roles, require_any_role
from models import Appointment, DentistCalendarConfig, AppointmentStatus, AppointmentAuditLog, GcalSyncStatus
from schemas import (
    AppointmentCreate, AppointmentUpdate, AppointmentResponse,
    AppointmentStatusUpdate, AppointmentListResponse
)
from utils.google_calendar import (
    get_calendar_service, get_free_busy, create_google_event,
    update_google_event, delete_google_event
)
from utils.crypto import decrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

_DEFAULT_DURATION_MINUTES = 30


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _get_dentist_config(db: Session, dentist_user_id: int, clinic_id: int):
    """Devuelve la config de GCal del dentista, o None si no tiene."""
    return db.query(DentistCalendarConfig).filter(
        and_(
            DentistCalendarConfig.dentist_user_id == dentist_user_id,
            DentistCalendarConfig.clinic_id == clinic_id,
        )
    ).first()


def _resolve_end_time(start: datetime, end: datetime | None, config) -> datetime:
    """Calcula end_time si no fue provisto, usando la duración default del dentista."""
    if end is not None:
        return end
    duration = (
        config.default_appointment_duration_minutes
        if config else _DEFAULT_DURATION_MINUTES
    )
    return start + timedelta(minutes=duration)


def _naive(dt: datetime) -> datetime:
    # SQLAlchemy DateTime sin timezone=True almacena UTC naive; strip tzinfo para
    # evitar TypeError al comparar con datetimes aware que vienen del exterior.
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _validate_times(start: datetime, end: datetime) -> None:
    s, e = _naive(start), _naive(end)
    if e <= s:
        raise HTTPException(status_code=422, detail="end_time_utc must be after start_time_utc")
    duration = (e - s).total_seconds()
    if duration < 900:
        raise HTTPException(status_code=422, detail="Appointment duration must be at least 15 minutes")
    if duration > 28800:
        raise HTTPException(status_code=422, detail="Appointment duration cannot exceed 8 hours")


def _check_overlap(db: Session, dentist_user_id: int, clinic_id: int,
                   start: datetime, end: datetime, exclude_id: int | None = None) -> None:
    filters = [
        Appointment.dentist_user_id == dentist_user_id,
        Appointment.clinic_id == clinic_id,
        Appointment.status == AppointmentStatus.SCHEDULED,
        Appointment.start_time_utc < end,
        Appointment.end_time_utc > start,
    ]
    if exclude_id:
        filters.append(Appointment.appointment_id != exclude_id)

    query = db.query(Appointment).filter(and_(*filters))

    # Pessimistic lock en MySQL para prevenir race conditions bajo carga concurrente.
    # SQLite (tests) no soporta FOR UPDATE — se detecta por el dialecto del engine.
    if hasattr(db, 'bind') and db.bind is not None and db.bind.dialect.name == 'mysql':
        query = query.with_for_update()

    if query.first():
        raise HTTPException(
            status_code=409,
            detail="This time slot is already booked for the selected dentist"
        )


def _sync_to_gcal(db: Session, apt: Appointment, config,
                  patient_email: str | None, action: str) -> None:
    """
    Sincroniza con Google Calendar y actualiza apt.gcal_sync_status.
    action: "create" | "update" | "delete".
    Nunca lanza excepción — la operación principal no debe fallar por GCal.
    El estado de sync queda registrado en gcal_sync_status para que el
    frontend pueda mostrar un badge de advertencia.
    """
    if not config or not config.google_refresh_token:
        return

    attendees = [patient_email] if patient_email else []
    summary   = f"Turno dental: {apt.patient_name or f'Paciente #{apt.patient_user_id}'}"

    extra_fields = [
        ("Paciente",  apt.patient_name),
        ("Email",     apt.patient_email),
        ("Teléfono",  apt.patient_phone),
        ("DNI",       apt.patient_dni),
        ("Dirección", apt.patient_address),
    ]
    lines = [f"Motivo: {apt.reason or 'Consulta odontológica'}"]
    lines += [f"{label}: {value}" for label, value in extra_fields if value]
    description = "\n".join(lines)

    try:
        access  = decrypt_token(config.google_access_token)
        refresh = decrypt_token(config.google_refresh_token)
        service = get_calendar_service(access, refresh, config.token_expiry)

        if action == "create":
            event_id = create_google_event(
                service, "primary",
                apt.start_time_utc, apt.end_time_utc,
                summary, description, attendees
            )
            apt.google_event_id   = event_id
            apt.gcal_sync_status  = GcalSyncStatus.SYNCED
            db.commit()
            logger.info(f"GCal event created: {event_id} for appointment {apt.appointment_id}")

        elif action == "update" and apt.google_event_id:
            update_google_event(
                service, "primary", apt.google_event_id,
                summary, apt.start_time_utc, apt.end_time_utc,
                description, attendees
            )
            apt.gcal_sync_status = GcalSyncStatus.SYNCED
            db.commit()
            logger.info(f"GCal event updated: {apt.google_event_id}")

        elif action == "delete" and apt.google_event_id:
            delete_google_event(service, "primary", apt.google_event_id)
            apt.google_event_id  = None
            apt.gcal_sync_status = GcalSyncStatus.NOT_CONFIGURED
            db.commit()
            logger.info(f"GCal event deleted for appointment {apt.appointment_id}")

    except Exception as e:
        logger.error(f"GCal sync failed (action={action}, appointment={apt.appointment_id}): {e}")
        if action in ("create", "update"):
            apt.gcal_sync_status = GcalSyncStatus.FAILED
            try:
                db.commit()
            except Exception:
                pass


def _add_audit(db: Session, apt: Appointment, user_id: int,
               previous_status: str, new_status: str, reason: str | None = None) -> None:
    db.add(AppointmentAuditLog(
        appointment_id=apt.appointment_id,
        changed_by_user_id=user_id,
        previous_status=previous_status,
        new_status=new_status,
        change_reason=reason
    ))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

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
    """Bloques ocupados del odontólogo (Google Calendar + turnos locales)."""
    config = _get_dentist_config(db, dentist_id, clinic_id)

    if not config or not config.google_refresh_token:
        raise HTTPException(status_code=400, detail="Dentist has not connected Google Calendar")

    try:
        service = get_calendar_service(
            decrypt_token(config.google_access_token),
            decrypt_token(config.google_refresh_token),
            config.token_expiry
        )
        google_busy = get_free_busy(service, "primary", start, end)

        local_busy = [
            {"start": a.start_time_utc.isoformat(), "end": a.end_time_utc.isoformat()}
            for a in db.query(Appointment).filter(
                and_(
                    Appointment.dentist_user_id == dentist_id,
                    Appointment.clinic_id == clinic_id,
                    Appointment.status == AppointmentStatus.SCHEDULED,
                    Appointment.start_time_utc >= start,
                    Appointment.end_time_utc <= end,
                )
            ).all()
        ]

        return {"dentist_id": dentist_id, "busy_slots": google_busy + local_busy}
    except Exception as e:
        logger.error(f"Error fetching availability for dentist {dentist_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch availability")


@router.get("/upcoming", response_model=AppointmentListResponse)
@limiter.limit("60/minute")
async def get_upcoming_appointments(
    request: Request,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
    limit: int = Query(5, ge=1, le=20),
):
    """Próximos turnos para el widget del dashboard."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB guarda naive UTC
    filters = [
        Appointment.clinic_id == clinic_id,
        Appointment.status == AppointmentStatus.SCHEDULED,
        Appointment.start_time_utc >= now,
    ]
    if "DENTIST" in roles and "ADMIN" not in roles:
        filters.append(Appointment.dentist_user_id == user_id)
    elif "PATIENT" in roles and "ADMIN" not in roles:
        filters.append(Appointment.patient_user_id == user_id)

    appointments = (
        db.query(Appointment)
        .filter(and_(*filters))
        .order_by(Appointment.start_time_utc.asc())
        .limit(limit)
        .all()
    )
    return {"appointments": appointments, "total": len(appointments)}


@router.get("/", response_model=AppointmentListResponse)
@limiter.limit("60/minute")
async def list_appointments(
    request: Request,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
    status: AppointmentStatus = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    patient_user_id: Optional[int] = Query(None),
):
    """
    Lista turnos de la clínica con filtros opcionales.
    patient_user_id: ADMIN/DENTIST/RECEPTIONIST pueden filtrar por paciente
    (ej. para mostrar turnos en la historia clínica del paciente).
    """
    filters = [Appointment.clinic_id == clinic_id]

    if patient_user_id and any(r in roles for r in ("ADMIN", "DENTIST", "RECEPTIONIST")):
        # Contexto historia clínica: mostrar todos los turnos del paciente en la clínica
        filters.append(Appointment.patient_user_id == patient_user_id)
    elif "ADMIN" not in roles:
        if "DENTIST" in roles:
            filters.append(Appointment.dentist_user_id == user_id)
        else:
            filters.append(Appointment.patient_user_id == user_id)

    if status:
        filters.append(Appointment.status == status)

    query = db.query(Appointment).filter(and_(*filters))
    total = query.count()
    appointments = (
        query.order_by(Appointment.start_time_utc.desc())
        .offset(offset).limit(limit).all()
    )
    return {"appointments": appointments, "total": total}


@router.get("/{appointment_id}", response_model=AppointmentResponse)
@limiter.limit("60/minute")
async def get_appointment(
    request: Request,
    appointment_id: int,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """Detalle de un turno."""
    apt = db.query(Appointment).filter(
        and_(Appointment.appointment_id == appointment_id, Appointment.clinic_id == clinic_id)
    ).first()
    if not apt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    if "ADMIN" not in roles:
        if "DENTIST" in roles:
            if apt.dentist_user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            if apt.patient_user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
    return apt


@router.post("/", response_model=AppointmentResponse,
             dependencies=[require_any_role("ADMIN", "RECEPTIONIST", "DENTIST")])
@limiter.limit("20/minute")
async def create_appointment(
    request: Request,
    apt_data: AppointmentCreate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
):
    """
    Crea un turno confirmado (SCHEDULED).
    Solo ADMIN o RECEPTIONIST pueden crear turnos.
    Si el odontólogo tiene Google Calendar conectado:
      - El turno se guarda en su GCal.
      - Si se provee patient_email, el paciente recibe una invitación automática.
    Si no tiene GCal conectado, el turno queda solo en nuestra base de datos.
    """
    config = _get_dentist_config(db, apt_data.dentist_user_id, clinic_id)

    start = apt_data.start_time_utc
    end = _resolve_end_time(start, apt_data.end_time_utc, config)
    _validate_times(start, end)
    _check_overlap(db, apt_data.dentist_user_id, clinic_id, start, end)

    apt = Appointment(
        clinic_id=clinic_id,
        dentist_user_id=apt_data.dentist_user_id,
        patient_user_id=apt_data.patient_user_id,
        patient_name=apt_data.patient_name,
        patient_email=apt_data.patient_email,
        patient_phone=apt_data.patient_phone,
        patient_dni=apt_data.patient_dni,
        patient_address=apt_data.patient_address,
        start_time_utc=start,
        end_time_utc=end,
        patient_timezone=apt_data.patient_timezone,
        reason=apt_data.reason,
        status=AppointmentStatus.SCHEDULED,
    )
    db.add(apt)
    db.commit()
    db.refresh(apt)

    logger.info(
        f"Appointment {apt.appointment_id} created for patient {apt_data.patient_user_id} "
        f"with dentist {apt_data.dentist_user_id} (clinic {clinic_id})"
    )

    # Sincronizar con Google Calendar (fallo silencioso — no revierte el turno)
    _sync_to_gcal(db, apt, config, apt_data.patient_email, action="create")
    if not config or not config.google_refresh_token:
        logger.info(f"Appointment {apt.appointment_id} saved to DB only — dentist has no GCal connected")

    db.refresh(apt)
    return apt


@router.put("/{appointment_id}", response_model=AppointmentResponse,
            dependencies=[require_any_role("ADMIN", "RECEPTIONIST", "DENTIST")])
@limiter.limit("20/minute")
async def update_appointment(
    request: Request,
    appointment_id: int,
    update_data: AppointmentUpdate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Edita fecha/hora y/o motivo de un turno SCHEDULED.
    DENTIST solo puede editar sus propios turnos.
    """
    apt = db.query(Appointment).filter(
        and_(Appointment.appointment_id == appointment_id, Appointment.clinic_id == clinic_id)
    ).first()
    if not apt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    if apt.status != AppointmentStatus.SCHEDULED:
        raise HTTPException(status_code=422, detail="Only SCHEDULED appointments can be edited")

    if "DENTIST" in roles and "ADMIN" not in roles and "RECEPTIONIST" not in roles:
        if apt.dentist_user_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

    new_start = _naive(update_data.start_time_utc) if update_data.start_time_utc else apt.start_time_utc
    new_end   = _naive(update_data.end_time_utc)   if update_data.end_time_utc   else apt.end_time_utc

    if update_data.start_time_utc or update_data.end_time_utc:
        _validate_times(new_start, new_end)
        _check_overlap(db, apt.dentist_user_id, clinic_id, new_start, new_end,
                       exclude_id=appointment_id)

    apt.start_time_utc = new_start
    apt.end_time_utc   = new_end
    if update_data.reason is not None:
        apt.reason = update_data.reason

    db.commit()
    db.refresh(apt)
    logger.info(f"Appointment {appointment_id} updated by user {user_id}")

    config = _get_dentist_config(db, apt.dentist_user_id, clinic_id)
    _sync_to_gcal(db, apt, config, patient_email=None, action="update")

    db.refresh(apt)
    return apt


@router.patch("/{appointment_id}/status", response_model=AppointmentResponse)
@limiter.limit("20/minute")
async def update_appointment_status(
    request: Request,
    appointment_id: int,
    status_update: AppointmentStatusUpdate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Cambia el estado de un turno.
    SCHEDULED → COMPLETED : solo el odontólogo asignado o ADMIN.
    SCHEDULED → CANCELLED : odontólogo, paciente, recepcionista o ADMIN.
    Si se cancela y tiene evento en GCal, lo elimina.
    """
    apt = db.query(Appointment).filter(
        and_(Appointment.appointment_id == appointment_id, Appointment.clinic_id == clinic_id)
    ).first()
    if not apt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    if apt.status != AppointmentStatus.SCHEDULED:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot change status of a {apt.status.value} appointment"
        )

    new_status = status_update.status
    if new_status not in (AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED):
        raise HTTPException(status_code=422, detail="Status must be COMPLETED or CANCELLED")

    is_admin       = "ADMIN" in roles
    is_own_dentist = "DENTIST" in roles and apt.dentist_user_id == user_id
    is_own_patient = "PATIENT" in roles and apt.patient_user_id == user_id
    is_receptionist = "RECEPTIONIST" in roles

    if new_status == AppointmentStatus.COMPLETED:
        if not is_admin and not is_own_dentist:
            raise HTTPException(status_code=403, detail="Only the assigned dentist or ADMIN can mark an appointment as completed")

    elif new_status == AppointmentStatus.CANCELLED:
        if not is_admin and not is_own_dentist and not is_own_patient and not is_receptionist:
            raise HTTPException(status_code=403, detail="Access denied")

    previous_status = apt.status.value
    apt.status = new_status
    _add_audit(db, apt, user_id, previous_status, new_status.value, status_update.change_reason)
    db.commit()
    db.refresh(apt)
    logger.info(f"Appointment {appointment_id}: {previous_status} → {new_status.value} by user {user_id}")

    if new_status == AppointmentStatus.CANCELLED:
        config = _get_dentist_config(db, apt.dentist_user_id, clinic_id)
        _sync_to_gcal(db, apt, config, patient_email=None, action="delete")

    db.refresh(apt)
    return apt
