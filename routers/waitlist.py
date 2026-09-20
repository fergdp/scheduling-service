"""
Lista de espera (#298): anotar a quien quiere un turno antes, y los avisos de los huecos que se
liberan. Las reglas viven en `lista_espera.py`; acá están los permisos y la forma de la API.

Dar el turno NO está acá: va por `POST /appointments` o `PUT /appointments/{id}` con
`waitlist_entry_id`, para reusar el solapamiento, la auditoría y Google Calendar sin copiarlos.

Los endpoints son `def` y no `async def` a propósito: SQLAlchemy acá es sincrónico, y adentro de
un `async def` una consulta lenta frena el event loop del worker entero —todas las clínicas—. Con
`def`, FastAPI los corre en su pool de hilos.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import and_, or_, true
from sqlalchemy.orm import Session

import lista_espera
from dependencies import (
    get_clinic_id, get_db, get_roles, get_user_id, require_any_role, telefonos_vigentes_de,
)
from models import FreedSlot, FreedSlotCloseReason, WaitlistEntry, WaitlistStatus
from routers import appointments as rutas_turnos
from schemas import (
    AppointmentBrief, FreedSlotListResponse, WaitlistEntryCreate, WaitlistEntryResponse,
    WaitlistEntryUpdate, WaitlistListResponse,
)

logger = logging.getLogger(__name__)

# Mismo motivo que `routers/appointments.py`: el teléfono vigente (#313) sale de `users`, tabla
# que la base de tests no tiene. Acá cubre el mismo bug en la lista de espera (#314).
_telefonos_vigentes = telefonos_vigentes_de

# ⚠️ Guard de rol en el ROUTER, no endpoint por endpoint: filtrar por `clinic_id` no alcanza,
# porque un PACIENTE también trae `clinic_id` en su JWT (#261). La lista tiene nombres y
# teléfonos de otros pacientes.
#
# `get_clinic_id` va PRIMERO: sin sesión tiene que salir 401, que es lo que hace que el front
# mande a iniciar sesión. Con el guard de rol adelante, un token vencido daba 403 (roles vacíos)
# y la pantalla se quedaba con un error en vez de pedir que vuelva a entrar.
router = APIRouter(dependencies=[
    Depends(get_clinic_id),
    require_any_role("ADMIN", "RECEPTIONIST", "DENTIST"),
])

# El mismo limitador que los turnos: los tests ya lo reinician entre caso y caso.
limiter = rutas_turnos.limiter

_SOLO_SU_LISTA = "Un odontólogo sólo puede anotar pacientes en su propia lista de espera"
_NO_ENCONTRADA = "Waiting list entry not found"


def _respuesta(entrada: WaitlistEntry, proximo=None, telefonos: dict | None = None) -> WaitlistEntryResponse:
    """
    La entrada como `WaitlistEntryResponse`, con `patient_phone` reemplazado por el vigente si
    `telefonos` tiene uno (#314). Si no, se deja el guardado al anotar: mejor un número viejo
    que ninguno — mismo criterio que `_con_telefono_vigente` en `routers/appointments.py`.
    """
    respuesta = WaitlistEntryResponse.model_validate(entrada, from_attributes=True)
    respuesta.next_appointment = AppointmentBrief.model_validate(proximo) if proximo is not None else None
    vigente = (telefonos or {}).get(entrada.patient_user_id)
    if vigente:
        respuesta.patient_phone = vigente
    return respuesta


def _cargar_entrada(db: Session, entry_id: int, clinic_id: int, roles: list[str], user_id: int) -> WaitlistEntry:
    """
    Una entrada que todavía espera, de esta clínica y visible para quien pide. 404 en cualquier
    otro caso, sin distinguir: lo más común es que otra persona ya le haya dado el turno.

    Sirve para validar y armar la respuesta. El cambio en sí va por `actualizar_si_espera`, que
    vuelve a mirar el estado en la base: entre esta lectura y el guardado la entrada puede cambiar.
    """
    entrada = db.query(WaitlistEntry).filter(and_(
        WaitlistEntry.entry_id == entry_id,
        WaitlistEntry.clinic_id == clinic_id,
        WaitlistEntry.status == WaitlistStatus.WAITING,
    )).first()
    if entrada is None or (lista_espera.solo_lo_suyo(roles) and entrada.dentist_user_id != user_id):
        raise HTTPException(status_code=404, detail=_NO_ENCONTRADA)
    return entrada


def _rechazar_duplicado(db: Session, clinic_id: int, patient_user_id: int,
                        dentist_user_id, excluir_id: int | None = None) -> None:
    """
    Un paciente no espera dos veces lo mismo: en el aviso aparecería dos veces, con dos botones
    para darle el mismo hueco.

    «Cualquier odontólogo» ya incluye a todos, así que choca con cualquier otra entrada del
    paciente, y una entrada para un odontólogo choca con la de ese odontólogo y con la de
    «cualquiera». Sí puede esperar a dos odontólogos distintos.
    """
    if dentist_user_id is None:
        mismo_alcance = true()
    else:
        mismo_alcance = or_(
            WaitlistEntry.dentist_user_id.is_(None),
            WaitlistEntry.dentist_user_id == dentist_user_id,
        )
    query = db.query(WaitlistEntry).filter(and_(
        WaitlistEntry.clinic_id == clinic_id,
        WaitlistEntry.patient_user_id == patient_user_id,
        WaitlistEntry.status == WaitlistStatus.WAITING,
        mismo_alcance,
    ))
    if excluir_id is not None:
        query = query.filter(WaitlistEntry.entry_id != excluir_id)
    if query.first():
        raise HTTPException(status_code=409, detail="Patient is already on the waiting list")


@router.get("/", response_model=WaitlistListResponse)
@limiter.limit("60/minute")
def list_waitlist(
    request: Request,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """Quienes esperan, en orden de llegada, cada uno con su próximo turno si tiene."""
    entradas = lista_espera.entradas_esperando(db, clinic_id=clinic_id, roles=roles, user_id=user_id)
    proximos = lista_espera.proximos_turnos(db, clinic_id=clinic_id, entradas=entradas)
    telefonos = _telefonos_vigentes({e.patient_user_id for e in entradas})
    return {
        "entries": [_respuesta(e, proximos.get(e.entry_id), telefonos) for e in entradas],
        "total": len(entradas),
    }


@router.post("/", response_model=WaitlistEntryResponse)
@limiter.limit("20/minute")
def add_to_waitlist(
    request: Request,
    data: WaitlistEntryCreate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Anotar a un paciente. El odontólogo sólo en su propia lista; «cualquier odontólogo» es de
    recepción y administración.
    """
    if lista_espera.solo_lo_suyo(roles) and data.dentist_user_id != user_id:
        raise HTTPException(status_code=403, detail=_SOLO_SU_LISTA)

    rutas_turnos._require_patient_of_clinic(data.patient_user_id, clinic_id)
    if data.dentist_user_id is not None:
        rutas_turnos._require_dentist_of_clinic(data.dentist_user_id, clinic_id)
    # Sin índice único detrás: dos altas iguales en el mismo instante pasarían las dos. El front
    # frena el doble click; un duplicado así se ve en la lista y se saca a mano.
    _rechazar_duplicado(db, clinic_id, data.patient_user_id, data.dentist_user_id)

    entrada = WaitlistEntry(
        clinic_id=clinic_id,
        patient_user_id=data.patient_user_id,
        patient_name=data.patient_name,
        patient_phone=data.patient_phone,
        dentist_user_id=data.dentist_user_id,
        available_from_utc=rutas_turnos._naive(data.available_from_utc),
        note=data.note,
        status=WaitlistStatus.WAITING,
        created_by_user_id=user_id,
    )
    db.add(entrada)
    db.commit()
    db.refresh(entrada)
    # Ids y nada más: el nombre y el teléfono del paciente no van al log.
    logger.info(f"Waitlist entry {entrada.entry_id} created for patient {data.patient_user_id} "
                f"(clinic {clinic_id}) by user {user_id}")

    proximos = lista_espera.proximos_turnos(db, clinic_id=clinic_id, entradas=[entrada])
    telefonos = _telefonos_vigentes({entrada.patient_user_id})
    return _respuesta(entrada, proximos.get(entrada.entry_id), telefonos)


@router.get("/slots", response_model=FreedSlotListResponse)
@limiter.limit("60/minute")
def list_freed_slots(
    request: Request,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Lo que muestra el cartel de la agenda: los huecos liberados que siguen libres y que alguien
    de la lista quiere, con sus candidatos en orden de llegada. Trae también cuántos esperan,
    para el contador del botón: la pantalla hace un solo pedido por recarga.
    """
    huecos = lista_espera.huecos_con_candidatos(db, clinic_id=clinic_id, roles=roles, user_id=user_id)
    # Un solo `IN` para todos los candidatos de TODOS los avisos, no uno por hueco.
    telefonos = _telefonos_vigentes(
        entrada.patient_user_id for h in huecos for entrada, _ in h.candidatos
    )
    return {
        "slots": [
            {
                "slot_id": h.aviso.slot_id,
                "dentist_user_id": h.aviso.dentist_user_id,
                "start_time_utc": h.aviso.start_time_utc,
                "end_time_utc": h.aviso.end_time_utc,
                "reason": h.aviso.reason,
                "created_at": h.aviso.created_at,
                "can_dismiss": h.puede_descartar,
                "candidates": [_respuesta(entrada, proximo, telefonos) for entrada, proximo in h.candidatos],
            }
            for h in huecos
        ],
        "waiting_count": lista_espera.contar_esperando(db, clinic_id=clinic_id, roles=roles, user_id=user_id),
    }


@router.post("/slots/{slot_id}/dismiss", status_code=204)
@limiter.limit("20/minute")
def dismiss_freed_slot(
    request: Request,
    slot_id: int,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Descartar un aviso: ya se decidió no ofrecer ese hueco. Se cierra para todos.

    Por eso el odontólogo sólo descarta un aviso que ve (404 si no, sin revelar si existe) y que
    es SÓLO suyo: si también tiene candidatos de la recepción, 403 — se lo cerraría a ella sin
    que se entere.
    """
    if lista_espera.solo_lo_suyo(roles):
        [hueco] = lista_espera.huecos_con_candidatos(
            db, clinic_id=clinic_id, roles=roles, user_id=user_id, slot_id=slot_id,
        ) or [None]
        if hueco is None:
            raise HTTPException(status_code=404, detail="Freed slot not found")
        if not hueco.puede_descartar:
            raise HTTPException(status_code=403, detail="Freed slot is shared with the front desk")

    # UPDATE condicional: si otra persona lo cerró entre tanto, no se pisa su cierre.
    filas = db.query(FreedSlot).filter(and_(
        FreedSlot.slot_id == slot_id,
        FreedSlot.clinic_id == clinic_id,
        FreedSlot.closed_at.is_(None),
    )).update({
        FreedSlot.closed_at: lista_espera.ahora_utc(),
        FreedSlot.closed_by_user_id: user_id,
        FreedSlot.close_reason: FreedSlotCloseReason.DISMISSED,
    }, synchronize_session=False)
    if not filas:
        raise HTTPException(status_code=404, detail="Freed slot not found")
    db.commit()
    logger.info(f"Freed slot {slot_id} dismissed by user {user_id} (clinic {clinic_id})")
    return Response(status_code=204)


@router.patch("/{entry_id}", response_model=WaitlistEntryResponse)
@limiter.limit("20/minute")
def update_waitlist_entry(
    request: Request,
    entry_id: int,
    data: WaitlistEntryUpdate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Cambiar el odontólogo, el día desde el que le sirve o la nota. Sólo cambia lo que viene en el
    pedido: `dentist_user_id: null` explícito es pasar a «cualquier odontólogo».
    """
    entrada = _cargar_entrada(db, entry_id, clinic_id, roles, user_id)
    campos = data.model_fields_set
    cambios = {}

    if "dentist_user_id" in campos:
        nuevo = data.dentist_user_id
        if lista_espera.solo_lo_suyo(roles) and nuevo != user_id:
            raise HTTPException(status_code=403, detail=_SOLO_SU_LISTA)
        if nuevo != entrada.dentist_user_id:
            if nuevo is not None:
                rutas_turnos._require_dentist_of_clinic(nuevo, clinic_id)
            _rechazar_duplicado(db, clinic_id, entrada.patient_user_id, nuevo, excluir_id=entrada.entry_id)
            cambios[WaitlistEntry.dentist_user_id] = nuevo

    if "available_from_utc" in campos:
        cambios[WaitlistEntry.available_from_utc] = rutas_turnos._naive(data.available_from_utc)
    if "note" in campos:
        cambios[WaitlistEntry.note] = data.note

    if cambios and not lista_espera.actualizar_si_espera(
        db, entry_id=entry_id, clinic_id=clinic_id, roles=roles, user_id=user_id, cambios=cambios,
    ):
        raise HTTPException(status_code=404, detail=_NO_ENCONTRADA)
    db.commit()
    db.refresh(entrada)
    logger.info(f"Waitlist entry {entry_id} updated by user {user_id} (clinic {clinic_id})")

    proximos = lista_espera.proximos_turnos(db, clinic_id=clinic_id, entradas=[entrada])
    telefonos = _telefonos_vigentes({entrada.patient_user_id})
    return _respuesta(entrada, proximos.get(entrada.entry_id), telefonos)


@router.delete("/{entry_id}", status_code=204)
@limiter.limit("20/minute")
def remove_from_waitlist(
    request: Request,
    entry_id: int,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """Sacar de la lista. Lógico: la fila queda como REMOVED, para el historial."""
    ahora = lista_espera.ahora_utc()
    if not lista_espera.actualizar_si_espera(
        db, entry_id=entry_id, clinic_id=clinic_id, roles=roles, user_id=user_id,
        cambios={
            WaitlistEntry.status: WaitlistStatus.REMOVED,
            WaitlistEntry.closed_at: ahora,
            WaitlistEntry.closed_by_user_id: user_id,
        },
    ):
        raise HTTPException(status_code=404, detail=_NO_ENCONTRADA)
    db.commit()
    logger.info(f"Waitlist entry {entry_id} removed by user {user_id} (clinic {clinic_id})")
    return Response(status_code=204)
