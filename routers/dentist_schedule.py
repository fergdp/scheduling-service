"""
Horario de atención semanal y bloqueos puntuales de agenda (#296). Las reglas de si un turno
entra o no en horario, o pisa un bloqueo, viven en `routers/appointments.py`
(`_check_dentro_de_horario`, `_check_sin_bloqueo`, `_turnos_activos_en_rango`) — acá sólo el
CRUD y los permisos de quién puede leer/editar el horario de quién.

Los endpoints son `def` y no `async def` a propósito, mismo motivo que `routers/waitlist.py`:
SQLAlchemy acá es sincrónico, y un `async def` frenaría el event loop del worker entero.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from dependencies import get_clinic_id, get_db, get_roles, get_user_id, require_any_role
from models import DentistScheduleBlock, DentistScheduleSlot
from routers import appointments as rutas_turnos
from schemas import (
    ScheduleBlockCreate, ScheduleBlockListResponse, ScheduleBlockResponse,
    ScheduleReplaceRequest, ScheduleResponse,
)

logger = logging.getLogger(__name__)

# Mismo razonamiento que `routers/waitlist.py`: el guard de rol va en el ROUTER — filtrar sólo
# por clinic_id no alcanza porque un PACIENTE también trae clinic_id en su JWT (#261). Acá
# además ningún endpoint es para PACIENTE: el horario de un odontólogo no es dato suyo.
router = APIRouter(dependencies=[
    Depends(get_clinic_id),
    require_any_role("ADMIN", "RECEPTIONIST", "DENTIST"),
])

# Mismo limitador que turnos y lista de espera — un solo bucket por usuario (#303).
limiter = rutas_turnos.limiter

_NO_AUTORIZADO = "Sólo el propio odontólogo o un admin pueden editar este horario"


def _require_self_or_admin(dentist_user_id: int, roles: list[str], user_id: int) -> None:
    """
    Decisión 3 del diseño del #296: edita el horario y los bloqueos el propio odontólogo (el
    suyo) o el admin (el de cualquiera). Recepción los ve reflejados en el calendario para dar
    turnos acorde, pero no los edita.
    """
    if "ADMIN" in roles:
        return
    if "DENTIST" in roles and dentist_user_id == user_id:
        return
    raise HTTPException(status_code=403, detail=_NO_AUTORIZADO)


@router.get("/dentists/{dentist_user_id}/schedule", response_model=ScheduleResponse)
@limiter.limit("60/minute")
def get_schedule(
    request: Request,
    dentist_user_id: int,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
):
    """El horario semanal del odontólogo. Lista vacía = disponible siempre (sin restricción)."""
    rutas_turnos._require_dentist_of_clinic(dentist_user_id, clinic_id)
    filas = db.query(DentistScheduleSlot).filter(
        DentistScheduleSlot.dentist_user_id == dentist_user_id,
        DentistScheduleSlot.clinic_id == clinic_id,
    ).order_by(DentistScheduleSlot.weekday, DentistScheduleSlot.start_time).all()
    return {"slots": filas}


@router.put("/dentists/{dentist_user_id}/schedule", response_model=ScheduleResponse)
@limiter.limit("20/minute")
def replace_schedule(
    request: Request,
    dentist_user_id: int,
    data: ScheduleReplaceRequest,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Reemplaza el horario semanal completo del odontólogo: se borran las filas viejas y se
    cargan las nuevas en la misma transacción. Mandar `slots: []` vuelve a «disponible
    siempre» (decisión 4 del diseño): borra la restricción, no dice «sin horario cargado».
    """
    _require_self_or_admin(dentist_user_id, roles, user_id)
    rutas_turnos._require_dentist_of_clinic(dentist_user_id, clinic_id)

    db.query(DentistScheduleSlot).filter(
        DentistScheduleSlot.dentist_user_id == dentist_user_id,
        DentistScheduleSlot.clinic_id == clinic_id,
    ).delete()
    nuevas = [
        DentistScheduleSlot(
            clinic_id=clinic_id,
            dentist_user_id=dentist_user_id,
            weekday=franja.weekday,
            start_time=franja.start_time,
            end_time=franja.end_time,
        )
        for franja in data.slots
    ]
    db.add_all(nuevas)
    db.commit()
    logger.info(
        f"Schedule replaced for dentist {dentist_user_id} ({len(nuevas)} slots) "
        f"by user {user_id} (clinic {clinic_id})"
    )

    filas = db.query(DentistScheduleSlot).filter(
        DentistScheduleSlot.dentist_user_id == dentist_user_id,
        DentistScheduleSlot.clinic_id == clinic_id,
    ).order_by(DentistScheduleSlot.weekday, DentistScheduleSlot.start_time).all()
    return {"slots": filas}


@router.get("/dentists/{dentist_user_id}/blocks", response_model=ScheduleBlockListResponse)
@limiter.limit("60/minute")
def list_blocks(
    request: Request,
    dentist_user_id: int,
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
):
    """Bloqueos del odontólogo que caen (aunque sea parcialmente) en el rango pedido."""
    rutas_turnos._require_dentist_of_clinic(dentist_user_id, clinic_id)
    inicio, fin = rutas_turnos._naive(from_), rutas_turnos._naive(to)
    filas = db.query(DentistScheduleBlock).filter(
        DentistScheduleBlock.dentist_user_id == dentist_user_id,
        DentistScheduleBlock.clinic_id == clinic_id,
        DentistScheduleBlock.start_time_utc < fin,
        DentistScheduleBlock.end_time_utc > inicio,
    ).order_by(DentistScheduleBlock.start_time_utc).all()
    return {"blocks": filas}


@router.post("/dentists/{dentist_user_id}/blocks", response_model=ScheduleBlockResponse)
@limiter.limit("20/minute")
def create_block(
    request: Request,
    dentist_user_id: int,
    data: ScheduleBlockCreate,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """
    Crea un bloqueo. Se rechaza (409) si pisa algún turno ACTIVO — decisión 2 del diseño:
    nada se reprograma ni se cancela solo, primero hay que hacerlo a mano.
    """
    _require_self_or_admin(dentist_user_id, roles, user_id)
    rutas_turnos._require_dentist_of_clinic(dentist_user_id, clinic_id)

    start = rutas_turnos._naive(data.start_time_utc)
    end = rutas_turnos._naive(data.end_time_utc)
    turnos_pisados = rutas_turnos._turnos_activos_en_rango(db, dentist_user_id, clinic_id, start, end)
    if turnos_pisados:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Hay turnos activos en ese horario. Reprogramalos o cancelalos antes de bloquear.",
                "conflicting_appointments": [
                    {
                        "appointment_id": t.appointment_id,
                        "start_time_utc": t.start_time_utc.isoformat(),
                        "end_time_utc": t.end_time_utc.isoformat(),
                    }
                    for t in turnos_pisados
                ],
            },
        )

    bloqueo = DentistScheduleBlock(
        clinic_id=clinic_id,
        dentist_user_id=dentist_user_id,
        start_time_utc=start,
        end_time_utc=end,
        reason=data.reason,
        created_by_user_id=user_id,
    )
    db.add(bloqueo)
    db.commit()
    db.refresh(bloqueo)
    logger.info(
        f"Schedule block {bloqueo.block_id} created for dentist {dentist_user_id} "
        f"by user {user_id} (clinic {clinic_id})"
    )
    return bloqueo


@router.delete("/dentists/{dentist_user_id}/blocks/{block_id}", status_code=204)
@limiter.limit("20/minute")
def delete_block(
    request: Request,
    dentist_user_id: int,
    block_id: int,
    db: Session = Depends(get_db),
    clinic_id: int = Depends(get_clinic_id),
    user_id: int = Depends(get_user_id),
    roles: list[str] = Depends(get_roles),
):
    """Borra un bloqueo. Físico: nada lo referencia por FK, no hace falta historial."""
    _require_self_or_admin(dentist_user_id, roles, user_id)
    rutas_turnos._require_dentist_of_clinic(dentist_user_id, clinic_id)
    filas = db.query(DentistScheduleBlock).filter(
        DentistScheduleBlock.block_id == block_id,
        DentistScheduleBlock.dentist_user_id == dentist_user_id,
        DentistScheduleBlock.clinic_id == clinic_id,
    ).delete()
    if not filas:
        raise HTTPException(status_code=404, detail="Schedule block not found")
    db.commit()
    logger.info(f"Schedule block {block_id} deleted by user {user_id} (clinic {clinic_id})")
    return Response(status_code=204)
