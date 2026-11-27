"""
Métricas de «A quién escribirle hoy» (#559, épica #555): saber si el panel sirve.

Todo sale de las marcas (`ContactMark`) que valen, las que nadie deshizo, de un período, cruzadas con
lo que pasó después:

| Motivo | Cuenta como «funcionó» si… |
|---|---|
| CONTROL | En los 30 días desde la marca (el mismo día incluido) se le creó un turno, en cualquier estado y sin borrar, o se le cargó una prestación. Es la regla de Weave: «agendó» es un turno dentro de las 4 semanas del aviso |
| PRESUPUESTO | En esos 30 días se aceptó (`accepted_at`) ese presupuesto o una versión nueva de él. Hoy está `ACCEPTED` o `CONVERTED` |
| RESENA | No se sabe: `wa.me` no avisa si el paciente la dejó. Se cuentan los pedidos, y las reseñas se cuentan a mano en Google, por semana |

- Una marca de hace menos de 30 días que todavía no funcionó está **en curso**: puede funcionar
  todavía. No se cuenta como fracaso; la pantalla la muestra aparte.
- ⚠️ **El porcentaje va sobre las marcas CUMPLIDAS** (`cumplidos`: pasaron los 30 días, el de la
  marca más 30, y hoy es después) y las que funcionaron entre ésas (`funcionaron_cumplidos`).
  `funcionaron / (escritos - en_curso)` daba de más: una reciente que ya funcionó entraba y una
  reciente que no, no (lo encontró la revisión independiente del #559).
- Se cuenta por marca, o sea por paciente: un mensaje a una madre por dos chicos son dos marcas. A un
  paciente que se le escribió dos veces en el período se lo cuenta dos veces, y cada vez por su lado.
- Los avisos **mostrados** no están acá: los cuenta la línea de log de `GET /hoy` (`contactos_hoy
  clinic_id=… avisos=…`), que va a Loki. No hace falta una tabla.

Lee, como el panel, tablas de otros servicios por SQL directo y con un GRANT por columna (#384):
`treatment_procedures.procedure_date` y `quote.accepted_at` (este último, desde el #559, con
`paso_559_grant.sh`). Si una lectura falla, esa cuenta sale en `None` y el motivo figura en
`motivos_con_error`: las demás salen igual.
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

import contactos_hoy
from horario_atencion import HUSO_CLINICA
from models import Appointment, ContactMark

logger = logging.getLogger(__name__)

VENTANA_DIAS = 30
PERIODO_DIAS = 90
PERIODO_DIAS_MAXIMO = 366

# Un presupuesto aceptado sigue contando cuando pasa a plan de tratamiento.
_ACEPTADO = ("ACCEPTED", "CONVERTED")


@dataclass
class MarcaParaMedir:
    patient_user_id: int
    motivo: str
    accion: str
    fecha: date
    quote_id: Optional[int] = None


@dataclass
class DelMotivo:
    escritos: int = 0
    # `None`: no se pudo leer con qué se cruza (ver `motivos_con_error`). RESENA nunca lo tiene.
    funcionaron: Optional[int] = 0
    en_curso: Optional[int] = 0
    cumplidos: Optional[int] = 0
    funcionaron_cumplidos: Optional[int] = 0
    pospuestos: int = 0
    descartados: int = 0


@dataclass
class Resultado:
    desde: date
    hasta: date
    por_motivo: dict = field(default_factory=dict)
    # `{lunes: {motivo: escritos}}`: para comparar los pedidos de reseña con las reseñas de Google.
    por_semana: dict = field(default_factory=dict)
    motivos_con_error: list = field(default_factory=list)


def _dia_en_cordoba(momento) -> Optional[date]:
    """Un `DATETIME` en UTC (como lo guardan los servicios) pasado al día de la clínica."""
    if momento is None:
        return None
    if isinstance(momento, str):
        momento = datetime.fromisoformat(momento)
    return momento.replace(tzinfo=timezone.utc).astimezone(HUSO_CLINICA).date()


def _en_ventana(marca: date, dia: Optional[date]) -> bool:
    return dia is not None and marca <= dia <= marca + timedelta(days=VENTANA_DIAS)


def leer_marcas_del_periodo(db: Session, clinic_id: int, desde: date, hasta: date) -> list:
    """Las marcas del período que valen. Una `DESHECHO` de después del período también anula."""
    filas = db.query(ContactMark).filter(
        ContactMark.clinic_id == clinic_id,
        ContactMark.fecha.between(desde, hasta),
        ContactMark.accion != "DESHECHO",
    ).all()
    if not filas:
        return []
    anuladas = {
        fila[0] for fila in db.query(ContactMark.anula_a_id).filter(
            ContactMark.clinic_id == clinic_id,
            ContactMark.anula_a_id.in_([m.mark_id for m in filas]),
        )
    }
    return [
        MarcaParaMedir(m.patient_user_id, m.motivo, m.accion, m.fecha, m.quote_id)
        for m in filas if m.mark_id not in anuladas
    ]


def leer_turnos_creados(db: Session, clinic_id: int, ids, desde: date, hasta: date) -> dict:
    """`{paciente: [día de Córdoba en que se creó cada turno]}`, de la base propia. Sin los borrados."""
    if not ids:
        return {}
    resultado = defaultdict(list)
    # Un día de margen a cada lado: `created_at` está en UTC y el día se calcula en Córdoba.
    for paciente, creado in db.query(Appointment.patient_user_id, Appointment.created_at).filter(
        Appointment.clinic_id == clinic_id,
        Appointment.patient_user_id.in_(sorted(ids)),
        Appointment.deleted_at.is_(None),
        Appointment.created_at >= desde - timedelta(days=1),
        Appointment.created_at < hasta + timedelta(days=VENTANA_DIAS + 2),
    ):
        dia = _dia_en_cordoba(creado)
        if dia is not None:
            resultado[paciente].append(dia)
    return resultado


def leer_prestaciones(clinic_id: int, ids, desde: date, hasta: date) -> dict:
    """`{paciente: [fecha de cada prestación]}`. Sólo la fecha: nada de qué se le hizo."""
    if not ids:
        return {}
    filas = contactos_hoy._consulta(
        "SELECT h.patient_user_id, tp.procedure_date "
        "FROM clinical_histories h JOIN treatment_procedures tp ON tp.history_id = h.history_id "
        "WHERE h.clinic_id = :c AND h.patient_user_id IN :ids AND tp.procedure_date BETWEEN :desde AND :hasta",
        {"c": clinic_id, "ids": sorted(ids), "desde": desde, "hasta": hasta + timedelta(days=VENTANA_DIAS)},
        "las prestaciones (métricas)",
    )
    resultado = defaultdict(list)
    for paciente, fecha in filas:
        dia = contactos_hoy._fecha(fecha)
        if dia is not None:
            resultado[paciente].append(dia)
    return resultado


def leer_aceptaciones(clinic_id: int, ids) -> dict:
    """
    `{quote_id: día en que se aceptó él o una versión suya}` para los presupuestos de esos pacientes.
    Una versión nueva (v2) cuenta para la v1 que se escribió: el odontólogo lo rehízo y el paciente
    aceptó ése. Sin montos ni ítems.
    """
    if not ids:
        return {}
    filas = contactos_hoy._consulta(
        f"SELECT id, version_of_quote_id, estado, accepted_at FROM {contactos_hoy.BASE_PRESUPUESTOS}.quote "
        f"WHERE clinic_id = :c AND patient_user_id IN :ids",
        {"c": clinic_id, "ids": sorted(ids)}, "los presupuestos aceptados",
    )
    raiz = {}
    aceptado_por_raiz = defaultdict(list)
    for quote_id, version_de, estado, aceptado in filas:
        r = version_de or quote_id
        raiz[quote_id] = r
        if estado in _ACEPTADO:
            dia = _dia_en_cordoba(aceptado)
            if dia is not None:
                aceptado_por_raiz[r].append(dia)
    return {q: aceptado_por_raiz.get(r, []) for q, r in raiz.items()}


def _lunes(dia: date) -> date:
    return dia - timedelta(days=dia.weekday())


def medir(db: Session, clinic_id: int, desde: date, hasta: date, hoy: date) -> Resultado:
    """Las cuentas del período `[desde, hasta]` (días de las marcas, incluidos los dos)."""
    resultado = Resultado(desde=desde, hasta=hasta)
    for motivo in ("CONTROL", "PRESUPUESTO", "RESENA"):
        r = DelMotivo()
        if motivo == "RESENA":
            _sin_saber(r)
        resultado.por_motivo[motivo] = r

    marcas = leer_marcas_del_periodo(db, clinic_id, desde, hasta)
    escritas = [m for m in marcas if m.accion == "ESCRITO"]
    for m in marcas:
        r = resultado.por_motivo[m.motivo]
        if m.accion == "ESCRITO":
            r.escritos += 1
            semana = resultado.por_semana.setdefault(_lunes(m.fecha), {"CONTROL": 0, "PRESUPUESTO": 0, "RESENA": 0})
            semana[m.motivo] += 1
        elif m.accion == "POSPUESTO":
            r.pospuestos += 1
        else:
            r.descartados += 1

    controles = [m for m in escritas if m.motivo == "CONTROL"]
    if controles:
        ids = {m.patient_user_id for m in controles}
        turnos = leer_turnos_creados(db, clinic_id, ids, desde, hasta)
        try:
            prestaciones = leer_prestaciones(clinic_id, ids, desde, hasta)
            con_error = False
        except contactos_hoy._LecturaFallida:
            # Sin las prestaciones, «volvió» contaría sólo los turnos: de menos. Mejor no decir nada.
            prestaciones, con_error = {}, True
        if con_error:
            resultado.motivos_con_error.append("CONTROL")
            _sin_saber(resultado.por_motivo["CONTROL"])
        else:
            _contar(resultado.por_motivo["CONTROL"], controles, hoy, lambda m: any(
                _en_ventana(m.fecha, d) for d in turnos.get(m.patient_user_id, []) + prestaciones.get(m.patient_user_id, [])
            ))

    presupuestos = [m for m in escritas if m.motivo == "PRESUPUESTO"]
    if presupuestos:
        try:
            aceptaciones = leer_aceptaciones(clinic_id, {m.patient_user_id for m in presupuestos})
        except contactos_hoy._LecturaFallida:
            resultado.motivos_con_error.append("PRESUPUESTO")
            _sin_saber(resultado.por_motivo["PRESUPUESTO"])
        else:
            _contar(resultado.por_motivo["PRESUPUESTO"], presupuestos, hoy, lambda m: any(
                _en_ventana(m.fecha, d) for d in aceptaciones.get(m.quote_id, [])
            ))

    logger.info(
        "contactos_hoy metricas clinic_id=%s desde=%s hasta=%s marcas=%s errores=%s",
        clinic_id, desde, hasta, len(marcas), ",".join(resultado.motivos_con_error),
    )
    return resultado


def _sin_saber(r: DelMotivo) -> None:
    """Lo que se cruza con otra tabla, sin dato: no se pudo leer, o (RESENA) no hay cómo saberlo."""
    r.funcionaron = r.en_curso = r.cumplidos = r.funcionaron_cumplidos = None


def _contar(r: DelMotivo, marcas: list, hoy: date, funciono) -> None:
    r.funcionaron = r.en_curso = r.cumplidos = r.funcionaron_cumplidos = 0
    for m in marcas:
        cumplida = m.fecha + timedelta(days=VENTANA_DIAS) < hoy
        si = funciono(m)
        r.funcionaron += si
        r.en_curso += not si and not cumplida
        r.cumplidos += cumplida
        r.funcionaron_cumplidos += si and cumplida
