"""
Horario de atención semanal y bloqueos de agenda (#296): si un rango de tiempo cabe dentro del
horario de un odontólogo y si pisa un bloqueo suyo.

`routers/appointments.py` lo usa para rechazar (409) un turno fuera de horario o sobre un
bloqueo. `lista_espera.py` (#333) lo usa para no OFRECER un hueco liberado que ese mismo chequeo
va a rechazar apenas alguien toque «Darle el turno» — antes se calculaba por separado y los dos
podían divergir.

Vive aparte de `routers/appointments.py` para que `lista_espera.py` lo pueda importar sin un
ciclo: ese router ya hace `import lista_espera`.
"""
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from models import DentistScheduleBlock, DentistScheduleSlot

# Un solo huso para todo el sistema, mismo supuesto implícito que ya usa el resto (el front
# muestra todo en el huso local del navegador sin que el backend necesite saberlo). Acá sí hace
# falta: es la única comparación que mira qué día/hora de la semana es, no sólo instantes UTC.
HUSO_CLINICA = ZoneInfo("America/Argentina/Buenos_Aires")


def naive(dt: datetime) -> datetime:
    """
    Pasa un datetime a UTC naive, que es como lo guardan las columnas (DateTime sin timezone).

    ⚠️ **CONVIERTE, no recorta.** `replace(tzinfo=None)` a secas guarda el reloj de pared: las
    10:00 de Buenos Aires quedarían como las 10:00 UTC, tres horas corridas — y el mismo instante
    escrito con dos husos distintos daría dos horas distintas, abriendo un agujero en el chequeo
    de solapamiento. Lo fija `tests/test_integridad_turnos.py`.
    """
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def cabe_en_horario(db: Session, dentist_user_id: int, clinic_id: int,
                    start: datetime, end: datetime) -> bool:
    """
    Si el odontólogo tiene AL MENOS UNA fila en `dentist_schedule_slots`, el rango tiene que
    caer entero dentro de UNA sola fila de ese día de la semana — así un rango que pisa el corte
    del mediodía entre dos franjas no cabe, no alcanza con que el inicio esté en una franja y el
    fin en otra. Sin ninguna fila cargada, siempre cabe: la restricción es opt-in por odontólogo
    (decisión 4 del diseño del #296).

    ⚠️ `start`/`end` llegan en UTC; `weekday`/`start_time`/`end_time` están en hora LOCAL de la
    clínica (`HUSO_CLINICA`). Hay que convertir ANTES de mirar día/hora — comparar el UTC crudo
    contra una franja local corre el resultado por el offset (encontrado por review, no en la
    primera versión: los tests armaban horario y turno con el mismo reloj por construcción y el
    huso nunca se ejercitaba).
    """
    start, end = naive(start), naive(end)
    tiene_horario = db.query(DentistScheduleSlot.slot_id).filter(
        DentistScheduleSlot.dentist_user_id == dentist_user_id,
        DentistScheduleSlot.clinic_id == clinic_id,
    ).first() is not None
    if not tiene_horario:
        return True

    inicio_local = start.replace(tzinfo=timezone.utc).astimezone(HUSO_CLINICA)
    fin_local = end.replace(tzinfo=timezone.utc).astimezone(HUSO_CLINICA)

    return db.query(DentistScheduleSlot.slot_id).filter(
        DentistScheduleSlot.dentist_user_id == dentist_user_id,
        DentistScheduleSlot.clinic_id == clinic_id,
        DentistScheduleSlot.weekday == inicio_local.weekday(),
        DentistScheduleSlot.start_time <= inicio_local.time(),
        DentistScheduleSlot.end_time >= fin_local.time(),
    ).first() is not None


def bloqueo_que_pisa(db: Session, dentist_user_id: int, clinic_id: int,
                     start: datetime, end: datetime) -> Optional[DentistScheduleBlock]:
    """El bloqueo de agenda del odontólogo (vacaciones, congreso) que pisa este rango, o None.
    Mismo predicado de solapamiento que el resto: pegados no se pisan."""
    start, end = naive(start), naive(end)
    return db.query(DentistScheduleBlock).filter(
        DentistScheduleBlock.dentist_user_id == dentist_user_id,
        DentistScheduleBlock.clinic_id == clinic_id,
        DentistScheduleBlock.start_time_utc < end,
        DentistScheduleBlock.end_time_utc > start,
    ).first()
