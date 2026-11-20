"""
«A quién escribirle hoy» (#556, épica #555): la lista diaria de pacientes a los que escribirles por
WhatsApp, con el motivo y el mensaje ya armado. El envío lo hace una persona, por `wa.me`: acá no
se manda nada.

Dos motivos automáticos (decisiones de Fernando, 2026-11-20):

| Motivo | Entra si… | No entra si… |
|---|---|---|
| PRESUPUESTO | Tiene un presupuesto `SENT` emitido hace entre 7 y 90 días, vencido o no | Tiene un turno futuro |
| CONTROL | Su última atención fue hace entre 180 y 1095 días | Tiene un turno futuro, o está en tratamiento de ortodoncia (`ACTIVO`, `EN_PAUSA` o `CONTENCION`: lo ve el ortodoncista) |

- «Última atención» es la prestación más nueva de su historia (`treatment_procedures`) o su último
  turno ya pasado que no se canceló ni faltó, la que sea más nueva. En la clínica 4 la agenda no se
  cierra (había 11 turnos `SCHEDULED` en el pasado y 3 `COMPLETED`, 2026-11-20): un `SCHEDULED` que
  ya pasó casi siempre es alguien que vino, y escribirle «ya pasaron 7 meses» la semana después de
  atenderlo es peor que demorarle un control. Una fecha futura (un error de tipeo) no cuenta.
- Si el paciente tiene un tratamiento de ortodoncia en otro estado (una consulta, por iniciar, en
  observación) sí entra: «Controles a agendar» (#354) sólo llama a los `ACTIVO`.
- Primero los presupuestos (el más viejo primero) y después los controles (el que hace menos que no
  viene primero: el más cercano al umbral es el que más vuelve).
- Un paciente aparece una sola vez por día. Un número de WhatsApp, también: los hermanos que van al
  mismo tutor salen en un solo aviso («uno por familia», como la planilla de Analía), y si dos
  motivos caen en el mismo número, hoy va el primero.
- Tope: 10 mensajes **por día**. Los que ya se marcaron «le escribí» hoy ocupan su lugar, y su número
  ya no recibe otro hoy (tampoco si hoy se le pidió la reseña). Un paciente con cualquier marca de hoy
  (pospuesto, descartado) no vuelve hoy por otro motivo. Uno con el teléfono mal cargado no ocupa
  lugar: va a `sin_telefono`.
- La vista de un odontólogo (`dentist_id`) es la lista de la clínica filtrada después de armarla:
  los dos ven los mismos avisos, y un número no recibe dos mensajes porque cada uno mira su parte.
- La reseña NO es un motivo de acá: es un botón manual (`POST /marcas` con `RESENA`). Pero un pedido
  de reseña cuenta como «le escribí» para el respiro de 7 días.

Marcas (`ContactMark`): «le escribí» saca al paciente de todo por 7 días y del control por 60; un
presupuesto escrito o descartado no vuelve; posponer lo saca de ese motivo hasta `hasta`; «no
sugerir nada» lo saca de todo, para siempre.

A quién se le escribe lo decide `contacto.a_quien_escribirle` (#417), la misma regla de los turnos
y de ortodoncia. Los dados de baja (#375) no aparecen. `is_enabled` no se mira, por lo mismo que en
#354: los que esperan aprobación están en falso.

Lee por SQL directo, sin API, tablas de otros servicios (ver «Tablas que otro servicio lee directo»
en el `CLAUDE.md` del monorepo). Con un GRANT por columna (#384): este servicio no puede leer la
descripción de una prestación ni los montos de un presupuesto. Si una lectura falla, el motivo que
depende de ella sale vacío y figura en `motivos_con_error`; los demás salen igual.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import and_, bindparam, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import contacto
import dependencies
from horario_atencion import HUSO_CLINICA
from models import Appointment, AppointmentStatus, ContactMark
from telefono_whatsapp import numero_para_whatsapp

logger = logging.getLogger(__name__)

# El nombre de la base de presupuestos. Vive en el mismo MySQL que la de dental-clinic: la consulta
# va por la conexión de `clinic_engine` con el nombre completo, como hace dental-clinic en el #375.
BASE_PRESUPUESTOS = "dental_quoting_db"

TOPE_DIARIO = 10
DIAS_CONTROL = 180
DIAS_CONTROL_MAXIMO = 3 * 365
PRESUPUESTO_DIAS_MINIMO = 7
PRESUPUESTO_DIAS_MAXIMO = 90
DIAS_RESPIRO = 7
DIAS_SIN_REPETIR_CONTROL = 60
POSPONER_DIAS = 7
POSPONER_DIAS_MAXIMO = 90
TOPE_SIN_TELEFONO = 20

# Los estados de ortodoncia (`OrthoEstado` de clinical-history) en los que el paciente va al
# ortodoncista y no se le escribe por un control general. Es una lista de los que SACAN, a propósito:
# un estado nuevo allá no deja a nadie sin control. ⚠️ Si se suma uno en tratamiento, va acá.
ORTO_EN_TRATAMIENTO = ("ACTIVO", "EN_PAUSA", "CONTENCION")

# Los turnos de antes que cuentan como «vino»: todos menos los cancelados y los ausentes.
_TURNO_QUE_CUENTA = (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED,
                     AppointmentStatus.ARRIVED, AppointmentStatus.COMPLETED)

# Los turnos que todavía tienen que venir: los que reservan el hueco y no empezaron.
_TURNO_POR_VENIR = (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)


def hoy_en_cordoba() -> date:
    """El día de la clínica. Nunca `date.today()`: el servidor está en UTC y a las 21 cambia de día."""
    return datetime.now(HUSO_CLINICA).date()


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Lo que se lee
# ---------------------------------------------------------------------------

@dataclass
class Presupuesto:
    quote_id: int
    patient_user_id: int
    dentista_user_id: Optional[int]
    fecha_emision: date
    fecha_vencimiento: Optional[date]


@dataclass
class Paciente:
    nombre: Optional[str]
    telefono: Optional[str]
    dado_de_baja: bool


@dataclass
class Marca:
    patient_user_id: int
    motivo: str
    accion: str
    fecha: date
    quote_id: Optional[int] = None
    hasta: Optional[date] = None


@dataclass
class Candidato:
    motivo: str
    patient_user_id: int
    dias: int = 0
    profesional_user_id: Optional[int] = None
    presupuesto: Optional[Presupuesto] = None


class _LecturaFallida(Exception):
    """Una tabla de otro servicio no se pudo leer. El mensaje de la base no viaja: puede traer datos."""


def _fecha(valor) -> Optional[date]:
    return contacto._fecha(valor)


def _consulta(sql: str, params: dict, que: str):
    """Una consulta a la base de dental-clinic. Los parámetros que son listas van como `IN :x`."""
    db = dependencies.ClinicSessionLocal()
    try:
        stmt = text(sql)
        listas = [k for k, v in params.items() if isinstance(v, (list, tuple))]
        if listas:
            stmt = stmt.bindparams(*(bindparam(k, expanding=True) for k in listas))
        return db.execute(stmt, params).all()
    except SQLAlchemyError as exc:
        logger.error("«A quién escribirle hoy»: no se pudo leer %s (%s)", que, type(exc).__name__)
        raise _LecturaFallida(que) from exc
    finally:
        db.close()


def leer_ultimas_prestaciones(clinic_id: int, hoy: date) -> dict:
    """
    De cada paciente de la clínica, su prestación más nueva: `{paciente: (fecha, profesional)}`.
    De dos del mismo día, la cargada después. Sólo columnas que no dicen nada clínico.

    Sólo las de los últimos tres años (más atrás nadie entra a Control) y ninguna con fecha futura:
    un error de tipeo no esconde al paciente.
    """
    filas = _consulta(
        "SELECT h.patient_user_id, tp.procedure_date, tp.professional_id, tp.procedure_id "
        "FROM clinical_histories h JOIN treatment_procedures tp ON tp.history_id = h.history_id "
        "WHERE h.clinic_id = :c AND tp.procedure_date BETWEEN :desde AND :hoy",
        {"c": clinic_id, "desde": hoy - timedelta(days=DIAS_CONTROL_MAXIMO), "hoy": hoy}, "las prestaciones",
    )
    mejores: dict = {}
    for paciente, fecha, profesional, procedure_id in filas:
        dia = _fecha(fecha)
        if dia is None:
            continue
        clave = (dia, procedure_id or 0)
        if paciente not in mejores or clave > mejores[paciente][0]:
            mejores[paciente] = (clave, profesional)
    return {p: (clave[0], profesional) for p, (clave, profesional) in mejores.items()}


def leer_ortodoncia_en_curso(clinic_id: int) -> set:
    """Los pacientes en tratamiento de ortodoncia (`ORTO_EN_TRATAMIENTO`): a ésos los ve el ortodoncista."""
    filas = _consulta(
        "SELECT DISTINCT patient_user_id FROM ortho_treatments "
        "WHERE clinic_id = :c AND estado IN :en_tratamiento",
        {"c": clinic_id, "en_tratamiento": list(ORTO_EN_TRATAMIENTO)}, "los tratamientos de ortodoncia",
    )
    return {fila[0] for fila in filas}


def leer_presupuestos(clinic_id: int, hoy: date) -> list:
    """
    Los presupuestos enviados y sin respuesta de la clínica, emitidos en la ventana. Vencidos
    también: «¿querés que te lo actualice?» (en la clínica 4, 4 de los 5 enviados estaban vencidos,
    2026-11-20). Uno que ya tiene una versión nueva no cuenta, en cualquier estado: si la v2 está en
    borrador, el odontólogo lo está rehaciendo; si se rechazó, ya hubo respuesta.
    """
    filas = _consulta(
        f"SELECT q.id, q.patient_user_id, q.dentista_user_id, q.fecha_emision, q.fecha_vencimiento "
        f"FROM {BASE_PRESUPUESTOS}.quote q "
        f"WHERE q.clinic_id = :c AND q.estado = 'SENT' AND q.patient_user_id IS NOT NULL "
        f"AND q.fecha_emision BETWEEN :desde AND :hasta "
        f"AND NOT EXISTS (SELECT 1 FROM {BASE_PRESUPUESTOS}.quote v "
        f"  WHERE v.clinic_id = q.clinic_id AND v.version_of_quote_id = COALESCE(q.version_of_quote_id, q.id) "
        f"  AND v.id > q.id)",
        {"c": clinic_id,
         "desde": hoy - timedelta(days=PRESUPUESTO_DIAS_MAXIMO),
         "hasta": hoy - timedelta(days=PRESUPUESTO_DIAS_MINIMO)},
        "los presupuestos",
    )
    return [
        Presupuesto(fila[0], fila[1], fila[2], _fecha(fila[3]), _fecha(fila[4]))
        for fila in filas if _fecha(fila[3]) is not None
    ]


def leer_duenos_de_presupuestos(quote_ids, clinic_id: int) -> dict:
    """`{quote_id: paciente}` de los presupuestos de la clínica: para marcar uno, tiene que ser de él."""
    if not quote_ids:
        return {}
    filas = _consulta(
        f"SELECT id, patient_user_id FROM {BASE_PRESUPUESTOS}.quote WHERE id IN :ids AND clinic_id = :c",
        {"ids": sorted(quote_ids), "c": clinic_id}, "los presupuestos",
    )
    return {fila[0]: fila[1] for fila in filas}


def leer_pacientes(ids, clinic_id: int) -> dict:
    """Nombre, teléfono y baja de los pacientes de la clínica. Uno de otra clínica no viene."""
    if not ids:
        return {}
    filas = _consulta(
        "SELECT user_id, first_name, last_name, phone, deactivated_at FROM users "
        "WHERE user_id IN :ids AND clinic_id = :c",
        {"ids": sorted(ids), "c": clinic_id}, "los pacientes",
    )
    return {
        fila[0]: Paciente(contacto._nombre(fila[1], fila[2]), contacto._texto(fila[3]), fila[4] is not None)
        for fila in filas
    }


def leer_nombre_clinica(clinic_id: int) -> Optional[str]:
    try:
        filas = _consulta("SELECT clinic_name FROM clinics WHERE clinic_id = :c", {"c": clinic_id}, "la clínica")
    except _LecturaFallida:
        return None  # El mensaje sale sin el nombre del consultorio.
    return contacto._texto(filas[0][0]) if filas else None


def leer_turnos(db: Session, clinic_id: int):
    """
    De la base propia: quiénes tienen un turno por venir, y el último turno ya pasado de cada uno que
    no se canceló ni faltó (`_TURNO_QUE_CUENTA`), de los últimos tres años.
    """
    ahora = _ahora_utc()
    futuros = {
        fila[0] for fila in db.query(Appointment.patient_user_id).filter(
            Appointment.clinic_id == clinic_id,
            Appointment.deleted_at.is_(None),
            Appointment.status.in_(_TURNO_POR_VENIR),
            Appointment.start_time_utc > ahora,
        ).distinct()
    }
    atendidos: dict = {}
    for paciente, inicio, odontologo in db.query(
        Appointment.patient_user_id, Appointment.start_time_utc, Appointment.dentist_user_id,
    ).filter(
        Appointment.clinic_id == clinic_id,
        Appointment.deleted_at.is_(None),
        Appointment.status.in_(_TURNO_QUE_CUENTA),
        Appointment.start_time_utc <= ahora,
        Appointment.start_time_utc >= ahora - timedelta(days=DIAS_CONTROL_MAXIMO + 1),
    ):
        dia = inicio.replace(tzinfo=timezone.utc).astimezone(HUSO_CLINICA).date()
        if paciente not in atendidos or dia > atendidos[paciente][0]:
            atendidos[paciente] = (dia, odontologo)
    return futuros, atendidos


def leer_marcas(db: Session, clinic_id: int, patient_user_ids=None, para_el_dia: Optional[date] = None) -> list:
    """
    Las marcas que valen: sin las `DESHECHO` ni las que una `DESHECHO` anuló.

    Con `para_el_dia`, sólo las que todavía pueden sacar a alguien ese día: las de los últimos 60 días,
    las de para siempre (no sugerir, un presupuesto escrito), los pospuestos que siguen y todas las
    `DESHECHO` (una puede anular a una vieja). Así no se lee la historia entera en cada pedido.
    """
    consulta = db.query(ContactMark).filter(ContactMark.clinic_id == clinic_id)
    if patient_user_ids is not None:
        consulta = consulta.filter(ContactMark.patient_user_id.in_(list(patient_user_ids)))
    if para_el_dia is not None:
        consulta = consulta.filter(or_(
            ContactMark.fecha > para_el_dia - timedelta(days=DIAS_SIN_REPETIR_CONTROL),
            ContactMark.accion.in_(("NO_SUGERIR", "NO_SUGERIR_NADA", "DESHECHO")),
            and_(ContactMark.motivo == "PRESUPUESTO", ContactMark.accion == "ESCRITO"),
            and_(ContactMark.accion == "POSPUESTO", ContactMark.hasta >= para_el_dia),
        ))
    filas = consulta.all()
    anuladas = {m.anula_a_id for m in filas if m.accion == "DESHECHO"}
    return [
        Marca(m.patient_user_id, m.motivo, m.accion, m.fecha, m.quote_id, m.hasta)
        for m in filas if m.accion != "DESHECHO" and m.mark_id not in anuladas
    ]


# ---------------------------------------------------------------------------
# Las reglas (sin base: se prueban con datos armados)
# ---------------------------------------------------------------------------

def ultimas_atenciones(prestaciones: dict, atendidos: dict) -> dict:
    """La más nueva entre la prestación y el turno atendido: `{paciente: (fecha, profesional)}`."""
    resultado = dict(prestaciones)
    for paciente, (dia, odontologo) in atendidos.items():
        # Con el mismo día gana la prestación: dice quién atendió de verdad.
        if paciente not in resultado or dia > resultado[paciente][0]:
            resultado[paciente] = (dia, odontologo)
    return resultado


@dataclass
class Bloqueos:
    nada: set = field(default_factory=set)
    respiro: set = field(default_factory=set)
    sin_control: set = field(default_factory=set)  # escrito por control hace menos de 60 días
    nunca_control: set = field(default_factory=set)
    presupuestos: set = field(default_factory=set)
    pospuestos: set = field(default_factory=set)  # (paciente, motivo)
    tocados_hoy: set = field(default_factory=set)  # con cualquier marca de hoy: no vuelven hoy
    escritos_hoy: set = field(default_factory=set)  # (paciente, motivo) con «le escribí» de hoy


def bloqueos(marcas: list, hoy: date) -> Bloqueos:
    b = Bloqueos()
    for m in marcas:
        if m.fecha == hoy:
            b.tocados_hoy.add(m.patient_user_id)
            if m.accion == "ESCRITO":
                b.escritos_hoy.add((m.patient_user_id, m.motivo))
        if m.accion == "NO_SUGERIR_NADA":
            b.nada.add(m.patient_user_id)
        elif m.accion == "ESCRITO":
            if (hoy - m.fecha).days < DIAS_RESPIRO:
                b.respiro.add(m.patient_user_id)
            if m.motivo == "CONTROL" and (hoy - m.fecha).days < DIAS_SIN_REPETIR_CONTROL:
                b.sin_control.add(m.patient_user_id)
            if m.motivo == "PRESUPUESTO" and m.quote_id is not None:
                b.presupuestos.add(m.quote_id)
        elif m.accion == "NO_SUGERIR":
            if m.motivo == "PRESUPUESTO" and m.quote_id is not None:
                b.presupuestos.add(m.quote_id)
            elif m.motivo == "CONTROL":
                b.nunca_control.add(m.patient_user_id)
        elif m.accion == "POSPUESTO" and m.hasta is not None and m.hasta >= hoy:
            b.pospuestos.add((m.patient_user_id, m.motivo))
    return b


def candidatos(
    hoy: date,
    atenciones: dict,
    presupuestos: list,
    ortodoncia_abierta: set,
    turno_futuro: set,
    marcas: list,
    dias_control: int = DIAS_CONTROL,
    con_presupuestos: bool = True,
    con_controles: bool = True,
) -> list:
    """
    Los candidatos del día, ya en el orden en que se muestran, un motivo por paciente. Falta mirar
    la baja y el teléfono, que necesitan leer a los pacientes: eso lo hace `armar_avisos`.
    """
    b = bloqueos(marcas, hoy)

    def libre(paciente: int, motivo: str) -> bool:
        return (paciente not in b.nada and paciente not in b.respiro and paciente not in b.tocados_hoy
                and paciente not in turno_futuro and (paciente, motivo) not in b.pospuestos)

    lista: list = []
    vistos: set = set()

    if con_presupuestos:
        for p in sorted(presupuestos, key=lambda p: (p.fecha_emision, p.quote_id)):
            if p.patient_user_id in vistos or p.quote_id in b.presupuestos:
                continue
            if not libre(p.patient_user_id, "PRESUPUESTO"):
                continue
            vistos.add(p.patient_user_id)
            lista.append(Candidato("PRESUPUESTO", p.patient_user_id, (hoy - p.fecha_emision).days,
                                   p.dentista_user_id, p))

    if con_controles:
        controles = []
        for paciente, (dia, profesional) in atenciones.items():
            dias = (hoy - dia).days
            if not (dias_control <= dias <= DIAS_CONTROL_MAXIMO):
                continue
            if paciente in vistos or paciente in ortodoncia_abierta:
                continue
            if paciente in b.sin_control or paciente in b.nunca_control or not libre(paciente, "CONTROL"):
                continue
            controles.append(Candidato("CONTROL", paciente, dias, profesional))
        controles.sort(key=lambda c: (c.dias, c.patient_user_id))
        lista.extend(controles)
    return lista


# ---------------------------------------------------------------------------
# El mensaje
# ---------------------------------------------------------------------------

def _capitalizar(palabra: str) -> str:
    """Como `capitalizar` de `whatsapp.js`: «JUAN» y «juan» → «Juan»; «McKenzie» se respeta."""
    if palabra == palabra.upper() or palabra == palabra.lower():
        return palabra[:1].upper() + palabra[1:].lower()
    return palabra


def primer_nombre(nombre: Optional[str]) -> str:
    partes = (nombre or "").split()
    return _capitalizar(partes[0]) if partes else ""


def _enumerar(nombres: list) -> str:
    nombres = [n for n in nombres if n]
    if len(nombres) <= 1:
        return nombres[0] if nombres else ""
    return ", ".join(nombres[:-1]) + " y " + nombres[-1]


def _ddmm(dia: date) -> str:
    return dia.strftime("%d/%m")


def mensaje(
    motivo: str,
    contacto_nombre: Optional[str],
    es_tutor: bool,
    pacientes: list,
    profesional: Optional[str],
    clinica: Optional[str],
    meses: int = 0,
    fecha_presupuesto: Optional[date] = None,
    presupuesto_vencido: bool = False,
) -> str:
    """
    El texto por defecto (los de cada clínica vienen con el #558). Voseo, «turno» y nunca «cita»,
    sin nombrar el tratamiento: los datos de salud son sensibles. Lo que no se sabe se omite.

    `pacientes`: los nombres de pila de a quiénes se nombra cuando quien lee es el tutor.
    """
    saludo = f"Hola {primer_nombre(contacto_nombre)}" if primer_nombre(contacto_nombre) else "Hola"
    donde = (clinica or "").strip().rstrip(".")
    if profesional:
        presentacion = f"Soy {profesional.strip()}{f', de {donde}' if donde else ''}."
        yo = "Quería"
    else:
        presentacion = f"Te escribimos de {donde}." if donde else ""
        yo = "Queríamos"
    inicio = f"{saludo}, ¿cómo estás?{f' {presentacion}' if presentacion else ''}"
    chicos = _enumerar(pacientes) if es_tutor else ""
    varios = es_tutor and len([p for p in pacientes if p]) > 1

    if motivo == "PRESUPUESTO":
        if es_tutor:
            de_quien = f" de {chicos}" if chicos else " de quien tenés a cargo"
        else:
            de_quien = ""
        cual = ("los presupuestos" if varios else "el presupuesto") + de_quien
        cuando = f" del {_ddmm(fecha_presupuesto)}" if fecha_presupuesto and not varios else ""
        texto = f"{inicio} {yo} saber si te quedó alguna duda con {cual}{cuando}. Si querés, lo vemos juntos."
        if presupuesto_vencido:
            texto += (" Como ya pasó la fecha de validez, te los podemos actualizar." if varios
                      else " Como ya pasó su fecha de validez, te lo podemos actualizar.")
        return texto

    tiempo = f"Ya pasaron {meses} meses desde " if meses > 0 else "Pasó un tiempo desde "
    if es_tutor:
        quien = chicos or "quien tenés a cargo"
        return (f"{inicio} {tiempo}la última consulta de {quien}: es un buen momento para un control. "
                f"¿Querés que {'les' if varios else 'le'} reservemos un turno?")
    return (f"{inicio} {tiempo}tu última consulta: es un buen momento para un control. "
            f"¿Querés que te reservemos un turno?")


# ---------------------------------------------------------------------------
# El armado completo
# ---------------------------------------------------------------------------

@dataclass
class Aviso:
    motivo: str
    numero: str
    contacto: dict
    profesional_user_id: Optional[int]
    pacientes: list = field(default_factory=list)  # [(Candidato, nombre)]


def _meses(dias: int) -> int:
    return dias // 30


def armar_avisos(
    hoy: date,
    lista: list,
    pacientes: dict,
    datos_contacto: dict,
    tope: int = TOPE_DIARIO,
    numeros_ocupados: frozenset = frozenset(),
) -> tuple:
    """
    De los candidatos en orden, los avisos del día: `(avisos, sin_telefono)`.

    - Un dado de baja, o uno que no es de la clínica (no está en `pacientes`), no sale.
    - Sin un número que se entienda va a `sin_telefono`, y no ocupa lugar del tope.
    - Los que van al mismo número por el mismo motivo y a un tutor se juntan en un aviso. Un adulto
      que comparte el número con otro adulto espera a otro día: el mensaje le habla a uno solo.
    - Un número ya usado hoy por otro motivo no recibe un segundo mensaje; uno de `numeros_ocupados`
      (al que ya se le escribió hoy), ninguno.
    """
    avisos: list = []
    por_clave: dict = {}
    numeros_usados: dict = {}
    sin_telefono: list = []

    for c in lista:
        paciente = pacientes.get(c.patient_user_id)
        if paciente is None or paciente.dado_de_baja:
            continue
        destino = contacto.contacto_de(c.patient_user_id, paciente.nombre, paciente.telefono, datos_contacto)
        numero = numero_para_whatsapp(destino.telefono)
        if numero is None:
            if len(sin_telefono) < TOPE_SIN_TELEFONO:
                sin_telefono.append((c, paciente.nombre))
            continue
        if numero in numeros_ocupados:
            continue
        motivo_del_numero = numeros_usados.get(numero)
        if motivo_del_numero is not None and motivo_del_numero != c.motivo:
            continue
        clave = (c.motivo, numero)
        existente = por_clave.get(clave)
        if existente is not None:
            if destino.es_tutor and existente.contacto["es_tutor"]:
                existente.pacientes.append((c, paciente.nombre))
            continue
        if len(avisos) >= tope:
            continue
        aviso = Aviso(c.motivo, numero, destino.model_dump(), c.profesional_user_id, [(c, paciente.nombre)])
        avisos.append(aviso)
        por_clave[clave] = aviso
        numeros_usados[numero] = c.motivo
    return avisos, sin_telefono


def razon(aviso: Aviso) -> str:
    c = aviso.pacientes[0][0]
    if aviso.motivo == "PRESUPUESTO":
        return f"Presupuesto del {_ddmm(c.presupuesto.fecha_emision)} sin respuesta"
    dias = min(p[0].dias for p in aviso.pacientes)
    verbo = "vienen" if len(aviso.pacientes) > 1 else "viene"
    meses = _meses(dias)
    return f"No {verbo} hace {meses} meses" if meses else f"No {verbo} hace {dias} días"


@dataclass
class ResultadoDelDia:
    hoy: date
    avisos: list
    sin_telefono: list
    motivos_con_error: list
    profesionales: dict
    clinica: Optional[str]


def armar_el_dia(
    db: Session,
    clinic_id: int,
    hoy: Optional[date] = None,
    dentist_id: Optional[int] = None,
    datos_de_contacto: Callable = contacto.datos_de_contacto,
    nombres_de: Callable = contacto.nombres_de,
) -> ResultadoDelDia:
    hoy = hoy or hoy_en_cordoba()
    errores: list = []

    try:
        prestaciones = leer_ultimas_prestaciones(clinic_id, hoy)
        ortodoncia = leer_ortodoncia_en_curso(clinic_id)
        con_controles = True
    except _LecturaFallida:
        prestaciones, ortodoncia, con_controles = {}, set(), False
        errores.append("CONTROL")
    try:
        presupuestos = leer_presupuestos(clinic_id, hoy)
        con_presupuestos = True
    except _LecturaFallida:
        presupuestos, con_presupuestos = [], False
        errores.append("PRESUPUESTO")

    futuros, atendidos = leer_turnos(db, clinic_id)
    marcas = leer_marcas(db, clinic_id, para_el_dia=hoy)
    lista = candidatos(hoy, ultimas_atenciones(prestaciones, atendidos), presupuestos, ortodoncia,
                       futuros, marcas, con_presupuestos=con_presupuestos, con_controles=con_controles)
    escritos_hoy = bloqueos(marcas, hoy).escritos_hoy

    ids = {c.patient_user_id for c in lista} | {p for p, _ in escritos_hoy}
    try:
        pacientes = leer_pacientes(ids, clinic_id)
    except _LecturaFallida:
        # Sin los pacientes no se sabe a quién escribir ni quién está dado de baja: nada sale.
        return ResultadoDelDia(hoy, [], [], ["CONTROL", "PRESUPUESTO"], {}, None)
    datos = datos_de_contacto(list(ids & set(pacientes)), clinic_id)

    # Lo que ya se mandó hoy: su número no recibe otro mensaje, y los del panel ocupan el tope.
    ocupados, del_panel = set(), set()
    for paciente_id, motivo in escritos_hoy:
        paciente = pacientes.get(paciente_id)
        if paciente is None:
            continue
        destino = contacto.contacto_de(paciente_id, paciente.nombre, paciente.telefono, datos)
        numero = numero_para_whatsapp(destino.telefono)
        if numero:
            ocupados.add(numero)
            if motivo != "RESENA":
                del_panel.add(numero)
    avisos, sin_telefono = armar_avisos(hoy, lista, pacientes, datos,
                                        tope=max(0, TOPE_DIARIO - len(del_panel)),
                                        numeros_ocupados=frozenset(ocupados))
    if dentist_id is not None:
        avisos = [a for a in avisos if a.profesional_user_id == dentist_id]
        sin_telefono = [(c, n) for c, n in sin_telefono if c.profesional_user_id == dentist_id]

    profesionales = nombres_de({a.profesional_user_id for a in avisos}, clinic_id)
    return ResultadoDelDia(hoy, avisos, sin_telefono, sorted(set(errores)), profesionales,
                           leer_nombre_clinica(clinic_id))
