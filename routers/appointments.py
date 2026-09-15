import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy import and_
from slowapi import Limiter
from slowapi.util import get_remote_address
from dependencies import (
    get_db, get_clinic_id, get_user_id, get_roles, require_any_role, _resolve_db_clinic_id,
)
from models import (
    Appointment, DentistCalendarConfig, AppointmentStatus, ACTIVE_STATUSES,
    AppointmentAuditLog, GcalSyncStatus,
)
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

# Estados desde los que un PACIENTE puede cancelar por el portal. Una vez que llegó a la
# sala de espera (ARRIVED) o el turno ya cerró, cancelar es cosa del staff.
_PATIENT_CANCELLABLE_FROM = (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)

# Clínica de un usuario según la base de dental-clinic (cacheado 60 s en dependencies).
# Es un nombre de módulo, no una llamada directa, para que los tests lo puedan reemplazar:
# la base de tests no tiene la tabla `users`.
_clinic_of_user = _resolve_db_clinic_id


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


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # DB guarda naive UTC


def _visibles(query):
    """
    Excluye los turnos con borrado lógico. Va en TODA lectura de `appointments`: un turno
    borrado no existe para la agenda, para el solapamiento ni para el widget del dashboard.
    La fila se conserva sólo porque appointment_audit_logs tiene FK a ella.
    """
    return query.filter(Appointment.deleted_at.is_(None))


def _load_appointment(db: Session, appointment_id: int, clinic_id: int) -> Appointment:
    """Turno de ESTA clínica, no borrado. 404 en cualquier otro caso (no se revela nada)."""
    apt = _visibles(db.query(Appointment).filter(
        and_(Appointment.appointment_id == appointment_id, Appointment.clinic_id == clinic_id)
    )).first()
    if not apt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return apt


def _is_staff_for(apt: Appointment, roles: list[str], user_id: int) -> bool:
    """
    Quiénes administran un turno: ADMIN, RECEPTIONIST (la agenda de toda la clínica es su
    trabajo) y el odontólogo asignado. Un odontólogo NO administra los turnos de un colega.
    """
    if "ADMIN" in roles or "RECEPTIONIST" in roles:
        return True
    return "DENTIST" in roles and apt.dentist_user_id == user_id


def _require_dentist_of_clinic(dentist_user_id: int, clinic_id: int) -> None:
    """
    El odontólogo tiene que existir y ser de esta clínica. Sin esto un id inventado deja un
    turno que no aparece en ninguna agenda y no cuenta para el solapamiento. No hay riesgo
    cross-tenant (el turno queda en la clínica del JWT igual): es integridad.
    """
    if _clinic_of_user(int(dentist_user_id)) != int(clinic_id):
        raise HTTPException(status_code=422, detail="Dentist not found in this clinic")


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
    # Sólo los estados ACTIVOS ocupan el hueco: un cancelado, un ausente o un atendido no
    # bloquean que se agende otro turno en ese horario.
    filters = [
        Appointment.dentist_user_id == dentist_user_id,
        Appointment.clinic_id == clinic_id,
        Appointment.status.in_(list(ACTIVE_STATUSES)),
        Appointment.deleted_at.is_(None),
        Appointment.start_time_utc < end,
        Appointment.end_time_utc > start,
    ]
    if exclude_id:
        filters.append(Appointment.appointment_id != exclude_id)

    query = db.query(Appointment).filter(and_(*filters))

    # Pessimistic lock en MySQL para prevenir race conditions bajo carga concurrente.
    # SQLite (tests) no soporta FOR UPDATE — se detecta por el dialecto del engine.
    # ⚠️ El lock vive hasta el commit: entre este chequeo y el commit del turno NO puede
    # haber ningún otro commit (la sync con Google, por ejemplo). Ver update_appointment.
    if hasattr(db, 'bind') and db.bind is not None and db.bind.dialect.name == 'mysql':
        query = query.with_for_update()

    if query.first():
        raise HTTPException(
            status_code=409,
            detail="This time slot is already booked for the selected dentist"
        )


def _gcal_description(apt: Appointment) -> tuple[str, str]:
    summary = f"Turno dental: {apt.patient_name or f'Paciente #{apt.patient_user_id}'}"
    extra_fields = [
        ("Paciente",  apt.patient_name),
        ("Email",     apt.patient_email),
        ("Teléfono",  apt.patient_phone),
        ("DNI",       apt.patient_dni),
        ("Dirección", apt.patient_address),
    ]
    lines = [f"Motivo: {apt.reason or 'Consulta odontológica'}"]
    lines += [f"{label}: {value}" for label, value in extra_fields if value]
    return summary, "\n".join(lines)


def _sync_to_gcal(db: Session, apt: Appointment, config,
                  patient_email: str | None, action: str) -> None:
    """
    Sincroniza con Google Calendar y actualiza apt.gcal_sync_status.
    action: "create" | "update" | "delete".
    Nunca lanza excepción — la operación principal no debe fallar por GCal.
    El estado de sync queda registrado en gcal_sync_status para que el
    frontend pueda mostrar un badge de advertencia.

    ⚠️ Hace commit. Llamarla siempre DESPUÉS del commit de la operación principal: un
    commit intermedio suelta el lock del solapamiento antes de persistir el cambio.
    """
    if not config or not config.google_refresh_token:
        return

    attendees = [patient_email] if patient_email else []
    summary, description = _gcal_description(apt)

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
        # También para "delete": si no se pudo sacar de Google, el evento sigue existiendo y
        # google_event_id se conserva. El badge FAILED lo cuenta, y un turno borrado con
        # google_event_id es la query de reconciliación.
        logger.error(f"GCal sync failed (action={action}, appointment={apt.appointment_id}): {e}")
        apt.gcal_sync_status = GcalSyncStatus.FAILED
        try:
            db.commit()
        except Exception:
            pass


def _delete_google_event_from(config, event_id: str, appointment_id: int) -> bool:
    """
    Borra un evento concreto del calendario de `config` (el del odontólogo ANTERIOR en una
    reasignación). No toca la fila: el evento ya no le pertenece al turno. Devuelve si salió.
    """
    if not config or not config.google_refresh_token or not event_id:
        return True
    try:
        service = get_calendar_service(
            decrypt_token(config.google_access_token),
            decrypt_token(config.google_refresh_token),
            config.token_expiry,
        )
        delete_google_event(service, "primary", event_id)
        logger.info(f"GCal event {event_id} deleted from previous dentist for appointment {appointment_id}")
        return True
    except Exception as e:
        logger.error(
            f"GCal delete of orphaned event {event_id} failed (appointment={appointment_id}): {e}"
        )
        return False


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
            for a in _visibles(db.query(Appointment).filter(
                and_(
                    Appointment.dentist_user_id == dentist_id,
                    Appointment.clinic_id == clinic_id,
                    Appointment.status.in_(list(ACTIVE_STATUSES)),
                    Appointment.start_time_utc >= start,
                    Appointment.end_time_utc <= end,
                )
            )).all()
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
    """Próximos turnos para el widget del dashboard (sólo los que ocupan el hueco)."""
    now = _utcnow_naive()
    filters = [
        Appointment.clinic_id == clinic_id,
        Appointment.status.in_(list(ACTIVE_STATUSES)),
        Appointment.start_time_utc >= now,
    ]
    if "DENTIST" in roles and "ADMIN" not in roles:
        filters.append(Appointment.dentist_user_id == user_id)
    elif "PATIENT" in roles and "ADMIN" not in roles:
        filters.append(Appointment.patient_user_id == user_id)

    appointments = (
        _visibles(db.query(Appointment).filter(and_(*filters)))
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
    # 500: una semana de 5 odontólogos a 24 turnos/día entra en una sola página.
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    patient_user_id: Optional[int] = Query(None),
    dentist_user_id: Optional[int] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
):
    """
    Lista turnos de la clínica con filtros opcionales.
    patient_user_id: ADMIN/DENTIST/RECEPTIONIST pueden filtrar por paciente
    (ej. para mostrar turnos en la historia clínica del paciente).
    dentist_user_id: filtrar la agenda por profesional (columna de la recepcionista).
    Un DENTIST sigue viendo sólo los suyos: el filtro se suma, no reemplaza al scoping.
    """
    filters = [Appointment.clinic_id == clinic_id]

    if patient_user_id and any(r in roles for r in ("ADMIN", "DENTIST", "RECEPTIONIST")):
        # Contexto historia clínica: mostrar todos los turnos del paciente en la clínica
        filters.append(Appointment.patient_user_id == patient_user_id)
    elif "ADMIN" not in roles and "RECEPTIONIST" not in roles:
        if "DENTIST" in roles:
            filters.append(Appointment.dentist_user_id == user_id)
        else:
            # PATIENT: solo sus propios turnos
            filters.append(Appointment.patient_user_id == user_id)

    if dentist_user_id:
        filters.append(Appointment.dentist_user_id == dentist_user_id)
    if date_from:
        filters.append(Appointment.start_time_utc >= _naive(date_from))
    if date_to:
        filters.append(Appointment.start_time_utc < _naive(date_to))
    if status:
        filters.append(Appointment.status == status)

    query = _visibles(db.query(Appointment).filter(and_(*filters)))
    total = query.count()
    # Orden ascendente cuando se filtra por fecha (agenda del día); descendente por defecto (historial)
    order = (
        Appointment.start_time_utc.asc()
        if date_from is not None
        else Appointment.start_time_utc.desc()
    )
    appointments = (
        query.order_by(order)
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
    """Detalle de un turno: el staff que lo administra o el paciente dueño."""
    apt = _load_appointment(db, appointment_id, clinic_id)

    is_own_patient = "PATIENT" in roles and apt.patient_user_id == user_id
    if not _is_staff_for(apt, roles, user_id) and not is_own_patient:
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
    _require_dentist_of_clinic(apt_data.dentist_user_id, clinic_id)
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
    Edita fecha/hora, motivo y/o odontólogo de un turno activo (SCHEDULED, CONFIRMED o ARRIVED).
    ADMIN y RECEPTIONIST editan cualquiera; DENTIST solo los suyos, y sólo el horario y el
    motivo: pasar un turno a la agenda de un colega es de ADMIN o RECEPTIONIST.
    Cambiar de odontólogo: solapamiento contra el nuevo, y el evento de Google Calendar
    sale del calendario del anterior y entra al del nuevo (si lo tiene conectado).
    """
    apt = _load_appointment(db, appointment_id, clinic_id)

    # Autorización ANTES de cualquier validación: un 422 con detalle a quien no puede ni ver
    # el turno le contaría en qué estado está.
    if not _is_staff_for(apt, roles, user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    old_dentist = apt.dentist_user_id
    new_dentist = update_data.dentist_user_id if update_data.dentist_user_id is not None else old_dentist
    dentist_changed = new_dentist != old_dentist
    if dentist_changed and "ADMIN" not in roles and "RECEPTIONIST" not in roles:
        raise HTTPException(
            status_code=403,
            detail="Only ADMIN or RECEPTIONIST can reassign an appointment to another dentist"
        )

    if apt.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="Only active appointments (SCHEDULED, CONFIRMED, ARRIVED) can be edited"
        )

    if dentist_changed:
        _require_dentist_of_clinic(new_dentist, clinic_id)

    new_start = _naive(update_data.start_time_utc) if update_data.start_time_utc else apt.start_time_utc
    new_end   = _naive(update_data.end_time_utc)   if update_data.end_time_utc   else apt.end_time_utc
    times_changed = bool(update_data.start_time_utc or update_data.end_time_utc)
    old_start, old_end = apt.start_time_utc, apt.end_time_utc

    if times_changed:
        _validate_times(new_start, new_end)
    if times_changed or dentist_changed:
        _check_overlap(db, new_dentist, clinic_id, new_start, new_end, exclude_id=appointment_id)

    # Todo el cambio de estado local va en UN commit, con el lock del solapamiento vivo.
    # Google se toca recién después: un commit intermedio soltaría el lock antes de
    # persistir la reasignación y otra reserva podría colarse en el hueco.
    old_event_id = apt.google_event_id
    old_config   = _get_dentist_config(db, old_dentist, clinic_id) if dentist_changed else None
    if dentist_changed:
        apt.dentist_user_id = new_dentist
        if old_event_id:
            # El evento vive en el calendario del anterior: deja de ser de este turno.
            apt.google_event_id  = None
            apt.gcal_sync_status = GcalSyncStatus.NOT_CONFIGURED
        _add_audit(
            db, apt, user_id, apt.status.value, apt.status.value,
            reason=f"Profesional cambiado: {old_dentist} → {new_dentist}"
                   + (f" (evento Google {old_event_id})" if old_event_id else ""),
        )
    if times_changed and (new_start != old_start or new_end != old_end):
        _add_audit(
            db, apt, user_id, apt.status.value, apt.status.value,
            reason=f"Horario cambiado: {old_start.isoformat()} → {new_start.isoformat()}",
        )

    apt.start_time_utc = new_start
    apt.end_time_utc   = new_end
    if update_data.reason is not None:
        apt.reason = update_data.reason

    db.commit()
    db.refresh(apt)
    logger.info(
        f"Appointment {appointment_id} updated by user {user_id}"
        + (f" (dentist {old_dentist} → {new_dentist})" if dentist_changed else "")
    )

    config = _get_dentist_config(db, apt.dentist_user_id, clinic_id)
    if dentist_changed:
        if old_event_id and not _delete_google_event_from(old_config, old_event_id, apt.appointment_id):
            # El id viejo quedó en la auditoría; el evento huérfano se limpia a mano.
            apt.gcal_sync_status = GcalSyncStatus.FAILED
            db.commit()
        # Evento nuevo en el calendario del odontólogo nuevo (con invitación al paciente).
        _sync_to_gcal(db, apt, config, apt.patient_email, action="create")
    else:
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
    - Staff (ADMIN, RECEPTIONIST, odontólogo asignado): a cualquier estado distinto del
      actual. No hay estados terminales: un atendido o un cancelado vuelve a programado.
    - PATIENT: sólo a CANCELLED, y sólo desde SCHEDULED o CONFIRMED.
    - Volver a un estado activo desde uno inactivo re-chequea solapamiento (409): el hueco
      pudo ocuparse mientras el turno estaba cancelado.
    Google Calendar: CANCELLED borra el evento; volver a activo lo recrea si no existe;
    COMPLETED y NO_SHOW no lo tocan.
    """
    apt = _load_appointment(db, appointment_id, clinic_id)

    new_status = status_update.status
    previous = apt.status

    # Autorización ANTES de mirar el estado: el 422 de "ya está en ese estado" le diría a
    # cualquiera de la clínica en qué estado está un turno ajeno.
    if not _is_staff_for(apt, roles, user_id):
        is_own_patient = "PATIENT" in roles and apt.patient_user_id == user_id
        if not is_own_patient:
            raise HTTPException(status_code=403, detail="Access denied")
        if new_status != AppointmentStatus.CANCELLED or previous not in _PATIENT_CANCELLABLE_FROM:
            raise HTTPException(
                status_code=403,
                detail="Patients can only cancel a scheduled or confirmed appointment"
            )

    if previous == new_status:
        raise HTTPException(status_code=422, detail=f"Appointment is already {new_status.value}")

    if new_status in ACTIVE_STATUSES and previous not in ACTIVE_STATUSES:
        _check_overlap(db, apt.dentist_user_id, clinic_id,
                       apt.start_time_utc, apt.end_time_utc, exclude_id=appointment_id)

    apt.status = new_status
    _add_audit(db, apt, user_id, previous.value, new_status.value, status_update.change_reason)
    db.commit()
    db.refresh(apt)
    logger.info(f"Appointment {appointment_id}: {previous.value} → {new_status.value} by user {user_id}")

    config = _get_dentist_config(db, apt.dentist_user_id, clinic_id)
    if new_status == AppointmentStatus.CANCELLED:
        _sync_to_gcal(db, apt, config, patient_email=None, action="delete")
    elif new_status in ACTIVE_STATUSES and previous not in ACTIVE_STATUSES and not apt.google_event_id:
        _sync_to_gcal(db, apt, config, apt.patient_email, action="create")

    db.refresh(apt)
    return apt


@router.delete("/{appointment_id}", status_code=204)
@limiter.limit("20/minute")
async def delete_appointment(
    request: Request,
    appointment_id: int,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Borrado lógico: "lo cargué mal". Distinto de cancelar, que queda en el historial.
    Staff (ADMIN, RECEPTIONIST, odontólogo asignado). Deja rastro en la auditoría y borra
    el evento de Google Calendar si existe. El turno deja de verse en todos los endpoints.
    """
    apt = _load_appointment(db, appointment_id, clinic_id)

    if not _is_staff_for(apt, roles, user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    apt.deleted_at = _utcnow_naive()
    apt.deleted_by_user_id = user_id
    _add_audit(db, apt, user_id, apt.status.value, "DELETED")
    db.commit()
    logger.info(f"Appointment {appointment_id} deleted (soft) by user {user_id}")

    config = _get_dentist_config(db, apt.dentist_user_id, clinic_id)
    _sync_to_gcal(db, apt, config, patient_email=None, action="delete")

    return Response(status_code=204)
