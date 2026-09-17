import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy import and_
from slowapi import Limiter
from dependencies import (
    get_db, get_clinic_id, get_user_id, get_roles, require_any_role, _resolve_db_clinic_id,
    telefonos_vigentes_de, key_por_usuario_o_ip,
)
import lista_espera
from models import (
    Appointment, DentistCalendarConfig, AppointmentStatus, ACTIVE_STATUSES,
    AppointmentAuditLog, FreedSlotReason, GcalSyncStatus,
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
# Por usuario, no por IP (#303): ver key_por_usuario_o_ip. `routers/waitlist.py` importa este
# mismo `limiter`, así que sus rutas quedan alcanzadas sin tocar nada ahí.
limiter = Limiter(key_func=key_por_usuario_o_ip)

_DEFAULT_DURATION_MINUTES = 30

# Estados desde los que un PACIENTE puede cancelar por el portal. Una vez que llegó a la
# sala de espera (ARRIVED) o el turno ya cerró, cancelar es cosa del staff.
_PATIENT_CANCELLABLE_FROM = (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)

# Clínica de un usuario según la base de dental-clinic (cacheado 60 s en dependencies).
# Es un nombre de módulo, no una llamada directa, para que los tests lo puedan reemplazar:
# la base de tests no tiene la tabla `users`.
_clinic_of_user = _resolve_db_clinic_id

# Mismo motivo: el teléfono vigente de cada paciente (issue #313) sale de `users`, tabla que la
# base de tests no tiene.
_telefonos_vigentes = telefonos_vigentes_de


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
    """
    Pasa un datetime a UTC naive, que es como lo guarda la columna (DateTime sin timezone).

    ⚠️ **CONVIERTE, no recorta.** Hacer `replace(tzinfo=None)` guarda el reloj de pared: las
    10:00 de Buenos Aires quedaban almacenadas como las 10:00 UTC, o sea tres horas corridas.
    Eso corría el turno en la agenda de todos Y abría un agujero en el chequeo de solapamiento,
    porque el mismo instante escrito con dos husos distintos daba dos horas distintas y los dos
    turnos entraban. Lo fija `tests/test_integridad_turnos.py`.
    """
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # DB guarda naive UTC


def _visibles(query):
    """
    Excluye los turnos con borrado lógico. Va en TODA lectura de `appointments`: un turno
    borrado no existe para la agenda, para el solapamiento ni para el widget del dashboard.
    La fila se conserva sólo porque appointment_audit_logs tiene FK a ella.
    """
    return query.filter(Appointment.deleted_at.is_(None))


def _orden_de_la_lista(con_rango: bool) -> tuple:
    """
    Orden del listado: hacia adelante cuando se pide un rango (la agenda), hacia atrás si no (el
    historial).

    ⚠️ El `appointment_id` de desempate no es cosmético. Cinco odontólogos a las 09:00 son cinco
    filas con el mismo horario, y con filas empatadas MySQL puede devolverlas en cualquier orden,
    distinto según el `LIMIT`/`OFFSET` de cada pedido (lo advierte su manual, en "LIMIT Query
    Optimization"). El calendario pagina (#288): sin desempate, un turno podía salir en dos
    páginas y otro en ninguna. SQLite no lo reproduce, por eso el test mira la consulta.
    """
    if con_rango:
        return (Appointment.start_time_utc.asc(), Appointment.appointment_id.asc())
    return (Appointment.start_time_utc.desc(), Appointment.appointment_id.desc())


def _armar_query_turno(db, appointment_id: int, clinic_id: int, con_lock: bool):
    """
    La consulta de un turno, separada para poder probar el lock sin base (como el solapamiento).

    ⚠️ Las mutaciones lo leen con `FOR UPDATE`. Sin lock, dos cambios a la vez sobre el mismo
    turno decidían cada uno sobre una foto vieja: adelantarlo desde la lista de espera mientras el
    paciente lo cancelaba desde el portal dejaba un turno CANCELADO movido al hueco, la entrada
    resuelta y el aviso cerrado con el horario libre (#298, visto intercalando las transacciones).
    """
    sesion = db if db is not None else Session()
    query = _visibles(sesion.query(Appointment).filter(
        and_(Appointment.appointment_id == appointment_id, Appointment.clinic_id == clinic_id)
    ))
    if con_lock:
        query = query.with_for_update()
    return query


def _load_appointment(db: Session, appointment_id: int, clinic_id: int, con_lock: bool = False) -> Appointment:
    """Turno de ESTA clínica, no borrado. 404 en cualquier otro caso (no se revela nada)."""
    apt = _armar_query_turno(db, appointment_id, clinic_id, con_lock).first()
    if not apt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return apt


def _con_telefono_vigente(apt: Appointment, telefonos: dict) -> AppointmentResponse:
    """
    El turno como `AppointmentResponse`, con `patient_phone` reemplazado por el vigente si
    `users` tiene uno (#313). Si no —paciente sin teléfono cargado hoy, o que ya no está en la
    clínica—, se deja el que quedó guardado en el turno: mejor un número viejo que ninguno.
    """
    resp = AppointmentResponse.model_validate(apt)
    vigente = telefonos.get(apt.patient_user_id)
    if vigente:
        resp.patient_phone = vigente
    return resp


def _con_telefonos_vigentes(appointments: list[Appointment]) -> list[AppointmentResponse]:
    """Versión en lote de `_con_telefono_vigente`: un solo `IN` para toda la página."""
    telefonos = _telefonos_vigentes({a.patient_user_id for a in appointments})
    return [_con_telefono_vigente(a, telefonos) for a in appointments]


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


def _require_patient_of_clinic(patient_user_id: int, clinic_id: int) -> None:
    """
    El paciente también tiene que ser de esta clínica. Era la asimetría del alta: el
    odontólogo se validaba y el paciente no, así que un id inventado dejaba un turno con un
    paciente que no existe —basura en la agenda— y, con `patient_email`, permitía disparar
    invitaciones de Google Calendar hacia direcciones arbitrarias desde el calendario del
    profesional.
    """
    if _clinic_of_user(int(patient_user_id)) != int(clinic_id):
        raise HTTPException(status_code=422, detail="Patient not found in this clinic")


def _validate_times(start: datetime, end: datetime) -> None:
    s, e = _naive(start), _naive(end)
    if e <= s:
        raise HTTPException(status_code=422, detail="end_time_utc must be after start_time_utc")
    duration = (e - s).total_seconds()
    if duration < 900:
        raise HTTPException(status_code=422, detail="Appointment duration must be at least 15 minutes")
    if duration > 28800:
        raise HTTPException(status_code=422, detail="Appointment duration cannot exceed 8 hours")


# Los dialectos que soportan `SELECT … FOR UPDATE`. MariaDB lo soporta igual que MySQL pero
# SQLAlchemy lo reporta con OTRO nombre: con `== 'mysql'` a secas, cambiar la cadena de conexión
# a `mariadb+pymysql://` apagaba el lock EN SILENCIO.
DIALECTOS_CON_LOCK = ("mysql", "mariadb")


def _armar_query_solapamiento(db, dentist_user_id: int, clinic_id: int,
                              start: datetime, end: datetime,
                              exclude_id: int | None, dialecto: str):
    """
    La consulta que busca turnos pisados, separada para poder probarla sin base de datos.

    El lock pesimista sólo corre en MySQL/MariaDB y los tests corren en SQLite, así que la
    protección contra la carrera entre dos reservas simultáneas NO se ejecuta nunca en la
    suite: se podía borrar entera y todo quedaba en verde. Con el dialecto como parámetro, el
    test compila la consulta contra MySQL y exige que el `FOR UPDATE` esté.
    """
    # Sólo los estados ACTIVOS ocupan el hueco: un cancelado, un ausente o un atendido no
    # bloquean que se agende otro turno en ese horario.
    filters = [
        Appointment.dentist_user_id == dentist_user_id,
        Appointment.clinic_id == clinic_id,
        Appointment.status.in_(list(ACTIVE_STATUSES)),
        Appointment.deleted_at.is_(None),
        # Pegados no se pisan: uno que termina 15:00 y otro que empieza 15:00 conviven.
        Appointment.start_time_utc < end,
        Appointment.end_time_utc > start,
    ]
    if exclude_id:
        # Mover un turno dentro de su propio horario no puede chocar contra sí mismo.
        filters.append(Appointment.appointment_id != exclude_id)

    sesion = db if db is not None else Session()
    query = sesion.query(Appointment).filter(and_(*filters))

    # ⚠️ El lock vive hasta el commit: entre este chequeo y el commit del turno NO puede
    # haber ningún otro commit (la sync con Google, por ejemplo). Ver update_appointment.
    if dialecto in DIALECTOS_CON_LOCK:
        query = query.with_for_update()
    return query


def _dialecto(db: Session) -> str:
    return db.bind.dialect.name if getattr(db, "bind", None) is not None else ""


def _con_lock(db: Session) -> bool:
    """Si esta base soporta `SELECT … FOR UPDATE`. Aparte para que el test pueda forzarlo en SQLite."""
    return _dialecto(db) in DIALECTOS_CON_LOCK


def _check_overlap(db: Session, dentist_user_id: int, clinic_id: int,
                   start: datetime, end: datetime, exclude_id: int | None = None) -> None:
    query = _armar_query_solapamiento(
        db, dentist_user_id, clinic_id, _naive(start), _naive(end), exclude_id, _dialecto(db),
    )

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

@router.get("/availability/dentist/{dentist_id}",
            dependencies=[Depends(get_clinic_id), require_any_role("ADMIN", "RECEPTIONIST", "DENTIST")])
@limiter.limit("30/minute")
async def get_dentist_availability(
    request: Request,
    dentist_id: int,
    start: datetime,
    end: datetime,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Bloques ocupados del odontólogo (Google Calendar + turnos locales).

    ⚠️ **Guard de rol obligatorio.** Los bloques salen del calendario `primary` de la cuenta
    Google PERSONAL del odontólogo, no de una agenda dental aparte: sin guard, cualquier
    PACIENTE de la clínica leía su vida privada. Filtrar por `clinic_id` no alcanza — un
    paciente también trae `clinic_id` en su JWT. Es la clase del issue #261.

    Y un odontólogo sólo puede consultar la suya: espiar la agenda de un colega no es parte
    de su trabajo.
    """
    if ("ADMIN" not in roles and "RECEPTIONIST" not in roles and dentist_id != user_id):
        raise HTTPException(status_code=403, detail="Access denied")

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
    # ⚠️ Falla CERRADO. La cadena de antes no tenía `else`, así que un token con un rol que
    # este servicio no conoce —el quinto rol que alguien agregue— veía la agenda entera de la
    # clínica. El default tiene que ser "sólo lo mío", como en `list_appointments`.
    if "ADMIN" in roles or "RECEPTIONIST" in roles:
        pass  # el staff de mostrador ve la agenda de toda la clínica
    elif "DENTIST" in roles:
        filters.append(Appointment.dentist_user_id == user_id)
    else:
        filters.append(Appointment.patient_user_id == user_id)

    appointments = (
        _visibles(db.query(Appointment).filter(and_(*filters)))
        .order_by(Appointment.start_time_utc.asc())
        .limit(limit)
        .all()
    )
    return {"appointments": _con_telefonos_vigentes(appointments), "total": len(appointments)}


@router.get("/", response_model=AppointmentListResponse)
@limiter.limit("60/minute")
async def list_appointments(
    request: Request,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
    # Repetible (#302): `?status=SCHEDULED&status=CONFIRMED` trae los de cualquiera de los dos.
    # Un solo `?status=X` sigue funcionando igual.
    status: Optional[list[AppointmentStatus]] = Query(None),
    # 500 es el tope por página, no por consulta: un mes de 5 odontólogos no entra, y tampoco
    # una semana con la agenda llena. El calendario pide las páginas que falten con `offset`
    # (#288), y por eso el orden tiene desempate (`_orden_de_la_lista`).
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    patient_user_id: Optional[int] = Query(None),
    dentist_user_id: Optional[int] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
):
    """
    Lista turnos de la clínica con filtros opcionales.

    `dentist_user_id`: filtrar la agenda por profesional (la columna de la recepcionista). Se
    SUMA al alcance: un odontólogo que filtre por un colega sigue sin ver nada ajeno.

    `status`: uno o varios estados (#302). «Por atender» son tres: programado, confirmado y en
    espera.

    `patient_user_id`: la solapa Turnos de la historia clínica. Acá el alcance por profesional
    **no se aplica a propósito**: el odontólogo que abre la ficha de un paciente necesita su
    historial de visitas completo, no sólo las suyas — una historia clínica a medias es peor
    que ninguna. No es una fuga: ese profesional ya puede abrir la ficha de cualquier paciente
    de su clínica, así que no accede a nada que no tuviera.

    ⚠️ Acá decía que el filtro "se suma, no reemplaza al scoping", que es lo contrario de lo
    que hace el código. Lo fija `test_permisos_turnos.py`, en las dos direcciones.
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
        filters.append(Appointment.status.in_(status))

    query = _visibles(db.query(Appointment).filter(and_(*filters)))
    total = query.count()
    appointments = (
        query.order_by(*_orden_de_la_lista(con_rango=date_from is not None))
        .offset(offset).limit(limit).all()
    )
    return {"appointments": _con_telefonos_vigentes(appointments), "total": total}


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
    return _con_telefono_vigente(apt, _telefonos_vigentes({apt.patient_user_id}))


# `get_clinic_id` antes que el guard de rol: sin sesión tiene que salir 401 (el front manda a
# iniciar sesión), no el 403 de «roles vacíos». Lo mismo en el PUT y en la disponibilidad.
@router.post("/", response_model=AppointmentResponse,
             dependencies=[Depends(get_clinic_id), require_any_role("ADMIN", "RECEPTIONIST", "DENTIST")])
@limiter.limit("20/minute")
async def create_appointment(
    request: Request,
    apt_data: AppointmentCreate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Crea un turno confirmado (SCHEDULED).

    Lo crean ADMIN, RECEPTIONIST y DENTIST. El odontólogo **sólo en su propia agenda**: el
    mostrador agenda para todos, el profesional para sí mismo.

    Si el odontólogo tiene Google Calendar conectado:
      - El turno se guarda en su GCal.
      - Si se provee patient_email, el paciente recibe una invitación automática.
    Si no tiene GCal conectado, el turno queda solo en nuestra base de datos.
    """
    # ⚠️ Sin este freno un odontólogo escribía en la agenda de un colega: le ocupaba el hueco
    # y le creaba un evento en su Google personal con invitación por mail — y después ni
    # siquiera podía leer el turno (el GET le da 403), así que no tenía cómo deshacerlo.
    # El PUT ya lo prohibía; el POST no.
    if ("ADMIN" not in roles and "RECEPTIONIST" not in roles
            and apt_data.dentist_user_id != user_id):
        raise HTTPException(
            status_code=403,
            detail="Un odontólogo sólo puede crear turnos en su propia agenda",
        )

    _require_dentist_of_clinic(apt_data.dentist_user_id, clinic_id)
    _require_patient_of_clinic(apt_data.patient_user_id, clinic_id)
    config = _get_dentist_config(db, apt_data.dentist_user_id, clinic_id)

    # A UTC naive ANTES de validar, chequear y guardar: si se guarda el valor con huso, la
    # columna se queda con el reloj de pared y el turno entra corrido (ver `_naive`).
    start = _naive(apt_data.start_time_utc)
    end = _naive(_resolve_end_time(start, apt_data.end_time_utc, config))
    _validate_times(start, end)

    # Lista de espera (#298): la entrada se toma ANTES del solapamiento, así una que ya no está
    # disponible corta sin haber bloqueado la agenda. Su lock evita que dos personas le den dos
    # turnos al mismo paciente a la vez.
    entrada = None
    if apt_data.waitlist_entry_id is not None:
        entrada = lista_espera.tomar_entrada(
            db, entry_id=apt_data.waitlist_entry_id, clinic_id=clinic_id,
            patient_user_id=apt_data.patient_user_id, roles=roles, user_id=user_id,
            con_lock=_con_lock(db),
        )

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
    if entrada is not None:
        db.flush()  # el id del turno nuevo, para anotarlo en la entrada
        lista_espera.resolver_entrada(entrada, appointment_id=apt.appointment_id, user_id=user_id)
    # El horario se ocupó: si había un aviso de hueco liberado ahí, ya no hay nada que ofrecer.
    lista_espera.cerrar_avisos_ocupados(
        db, clinic_id=clinic_id, dentist_user_id=apt_data.dentist_user_id,
        start=start, end=end, user_id=user_id,
    )
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
    return _con_telefono_vigente(apt, _telefonos_vigentes({apt.patient_user_id}))


@router.put("/{appointment_id}", response_model=AppointmentResponse,
            dependencies=[Depends(get_clinic_id), require_any_role("ADMIN", "RECEPTIONIST", "DENTIST")])
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
    # Lista de espera (#298): el lock de la entrada va ANTES que el del turno, en el mismo orden
    # que el alta (ver `lista_espera`). Se valida más abajo, cuando ya se sabe de quién es el turno.
    entrada_bloqueada = None
    if update_data.waitlist_entry_id is not None:
        entrada_bloqueada = lista_espera.bloquear_entrada(
            db, entry_id=update_data.waitlist_entry_id, clinic_id=clinic_id, con_lock=_con_lock(db),
        )
    apt = _load_appointment(db, appointment_id, clinic_id, con_lock=_con_lock(db))

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

    old_start, old_end = apt.start_time_utc, apt.end_time_utc
    new_start = _naive(update_data.start_time_utc) if update_data.start_time_utc else old_start
    if update_data.end_time_utc:
        new_end = _naive(update_data.end_time_utc)
    elif update_data.start_time_utc:
        # Correr el inicio sin decir nada del fin MUEVE el turno: conserva su duración. Antes
        # se quedaba con el fin viejo, así que adelantarlo media hora lo hacía durar el doble
        # y bloqueaba agenda que nadie pidió bloquear.
        new_end = new_start + (old_end - old_start)
    else:
        new_end = old_end
    times_changed = bool(update_data.start_time_utc or update_data.end_time_utc)

    if times_changed:
        _validate_times(new_start, new_end)
    movido = dentist_changed or new_start != old_start or new_end != old_end

    # Lista de espera (#298), «Adelantar su turno»: el paciente de este turno sale de la lista en
    # el mismo commit en que su turno se mueve. Sin movimiento no hay nada que adelantar.
    entrada = None
    if update_data.waitlist_entry_id is not None:
        if not movido:
            raise HTTPException(
                status_code=422,
                detail="Waiting list entry requires moving the appointment",
            )
        entrada = lista_espera.validar_entrada(
            entrada_bloqueada, patient_user_id=apt.patient_user_id, roles=roles, user_id=user_id,
        )

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

    if movido:
        # El horario viejo quedó libre: aviso para la lista de espera. Salvo que el turno siga
        # pisándolo (lo corrieron un rato, o lo estiraron): ahí no se liberó un hueco que se
        # pueda ofrecer. Y el horario nuevo se ocupó: sus avisos se cierran.
        sigue_pisando = not dentist_changed and new_start < old_end and new_end > old_start
        if not sigue_pisando:
            lista_espera.registrar_hueco_liberado(
                db, clinic_id=clinic_id, dentist_user_id=old_dentist, start=old_start, end=old_end,
                source_appointment_id=apt.appointment_id, reason=FreedSlotReason.RESCHEDULED,
                user_id=user_id,
            )
        lista_espera.cerrar_avisos_ocupados(
            db, clinic_id=clinic_id, dentist_user_id=new_dentist, start=new_start, end=new_end,
            user_id=user_id,
        )
    if entrada is not None:
        lista_espera.resolver_entrada(entrada, appointment_id=apt.appointment_id, user_id=user_id)

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
        quedo_huerfano = bool(old_event_id) and not _delete_google_event_from(
            old_config, old_event_id, apt.appointment_id
        )
        # Evento nuevo en el calendario del odontólogo nuevo (con invitación al paciente).
        _sync_to_gcal(db, apt, config, apt.patient_email, action="create")
        if quedo_huerfano:
            # ⚠️ Va DESPUÉS de crear el evento nuevo, no antes: `_sync_to_gcal` deja SYNCED al
            # crear y pisaba este FAILED. El evento viejo sigue vivo en el calendario del
            # odontólogo anterior —el paciente aparece con dos citas— y el único rastro que ve
            # la recepcionista es este estado. El id viejo queda en la auditoría.
            apt.gcal_sync_status = GcalSyncStatus.FAILED
            db.commit()
    else:
        _sync_to_gcal(db, apt, config, patient_email=None, action="update")

    db.refresh(apt)
    return _con_telefono_vigente(apt, _telefonos_vigentes({apt.patient_user_id}))


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
    apt = _load_appointment(db, appointment_id, clinic_id, con_lock=_con_lock(db))

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

    # Lista de espera (#298). Cancelar un turno que ocupaba el hueco lo libera, lo cancele quien
    # lo cancele —también el paciente desde su portal, que es justo el caso del que nadie se
    # enteraba—. Reactivarlo lo vuelve a ocupar, y así el Deshacer de una cancelación se lleva
    # también el aviso. Ausente y atendido no avisan: son turnos del pasado.
    if new_status == AppointmentStatus.CANCELLED and previous in ACTIVE_STATUSES:
        lista_espera.registrar_hueco_liberado(
            db, clinic_id=clinic_id, dentist_user_id=apt.dentist_user_id,
            start=apt.start_time_utc, end=apt.end_time_utc,
            source_appointment_id=apt.appointment_id, reason=FreedSlotReason.CANCELLED,
            user_id=user_id,
        )
    elif new_status in ACTIVE_STATUSES and previous not in ACTIVE_STATUSES:
        lista_espera.cerrar_avisos_ocupados(
            db, clinic_id=clinic_id, dentist_user_id=apt.dentist_user_id,
            start=apt.start_time_utc, end=apt.end_time_utc, user_id=user_id,
        )
    db.commit()
    db.refresh(apt)
    logger.info(f"Appointment {appointment_id}: {previous.value} → {new_status.value} by user {user_id}")

    config = _get_dentist_config(db, apt.dentist_user_id, clinic_id)
    if new_status == AppointmentStatus.CANCELLED:
        _sync_to_gcal(db, apt, config, patient_email=None, action="delete")
    elif new_status in ACTIVE_STATUSES and previous not in ACTIVE_STATUSES and not apt.google_event_id:
        _sync_to_gcal(db, apt, config, apt.patient_email, action="create")

    db.refresh(apt)
    return _con_telefono_vigente(apt, _telefonos_vigentes({apt.patient_user_id}))


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
    apt = _load_appointment(db, appointment_id, clinic_id, con_lock=_con_lock(db))

    if not _is_staff_for(apt, roles, user_id):
        raise HTTPException(status_code=403, detail="Access denied")

    apt.deleted_at = _utcnow_naive()
    apt.deleted_by_user_id = user_id
    _add_audit(db, apt, user_id, apt.status.value, "DELETED")
    # Lista de espera (#298): borrar no avisa («lo cargué mal»). Y si algún aviso abierto quedó
    # pisado por este turno —otra transacción lo registró mientras ésta esperaba su lock—, se
    # cierra ahora: si no, reaparecería al desaparecer el turno que lo tapaba.
    if apt.status in ACTIVE_STATUSES:
        lista_espera.cerrar_avisos_ocupados(
            db, clinic_id=clinic_id, dentist_user_id=apt.dentist_user_id,
            start=apt.start_time_utc, end=apt.end_time_utc, user_id=user_id,
        )
    # Y los avisos que había dejado este turno (cancelado o movido antes): estaba mal cargado.
    lista_espera.cerrar_avisos_del_turno_borrado(
        db, clinic_id=clinic_id, appointment_id=apt.appointment_id, user_id=user_id,
    )
    db.commit()
    logger.info(f"Appointment {appointment_id} deleted (soft) by user {user_id}")

    config = _get_dentist_config(db, apt.dentist_user_id, clinic_id)
    _sync_to_gcal(db, apt, config, patient_email=None, action="delete")

    return Response(status_code=204)
