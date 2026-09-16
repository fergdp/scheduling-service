"""
La lista de espera (#298): quién espera un turno antes, qué huecos se liberaron y a quién se le
ofrece cada uno.

La usan los dos routers. El de turnos registra un hueco al cancelar o reprogramar, cierra los
avisos cuando el horario se vuelve a ocupar y resuelve la entrada cuando le da el turno a alguien
de la lista. El de la lista muestra todo.

⚠️ **Nada de acá hace commit.** Cada función agrega o cambia filas dentro de la transacción de
quien la llama: el turno y la lista cambian juntos o no cambian. Un commit acá adentro, además,
soltaría el lock del chequeo de solapamiento antes de guardar el turno (ver `update_appointment`).

⚠️ **Orden de los locks**: la entrada de la lista, después los turnos. Los dos caminos que toman
los dos (dar un turno nuevo y adelantar uno) los piden en ese orden, así no se esperan en cruz.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, true
from sqlalchemy.orm import Session

from models import (
    ACTIVE_STATUSES, Appointment, FreedSlot, FreedSlotCloseReason, FreedSlotReason,
    WaitlistEntry, WaitlistStatus,
)

# Lo más largo que puede durar un turno (`_validate_times`, en el router de turnos). Sirve de cota
# para buscar avisos que se pisan con un horario: uno que empieza más de 8 h antes no llega.
DURACION_MAXIMA_TURNO = timedelta(hours=8)

# Cuántos avisos devuelve el cartel como mucho. Los de más adelante aparecen cuando se resuelven
# los primeros: una recepcionista no va a ofrecer 50 huecos a la vez, y sin tope un aviso lejano
# por cada turno movido hacía crecer la consulta sin límite.
LIMITE_AVISOS = 50


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # la base guarda UTC naive


def solo_lo_suyo(roles: list[str]) -> bool:
    """
    Quién ve sólo su propia lista: todo el que no sea de mostrador. El router ya deja pasar
    únicamente a ADMIN, RECEPTIONIST y DENTIST, así que en la práctica es el odontólogo.

    ⚠️ Falla CERRADO a propósito: un rol que este servicio no conozca queda acotado a lo suyo,
    no ve la clínica entera. Es el agujero de las cadenas `if/elif` sin `else` del #294.
    """
    return not ("ADMIN" in roles or "RECEPTIONIST" in roles)


def _se_pisan(inicio_a: datetime, fin_a: datetime, inicio_b: datetime, fin_b: datetime) -> bool:
    # Misma regla que el solapamiento de turnos: pegados no se pisan.
    return inicio_a < fin_b and fin_a > inicio_b


def _turnos_activos(db: Session, clinic_id: int):
    """Turnos que ocupan un hueco: activos y no borrados."""
    return db.query(Appointment).filter(and_(
        Appointment.clinic_id == clinic_id,
        Appointment.status.in_(list(ACTIVE_STATUSES)),
        Appointment.deleted_at.is_(None),
    ))


# ---------------------------------------------------------------------------
# Los avisos de hueco liberado
# ---------------------------------------------------------------------------

def _avisos_abiertos_que_pisan(db: Session, clinic_id: int, dentist_user_id: int,
                               start: datetime, end: datetime) -> list[FreedSlot]:
    return db.query(FreedSlot).filter(and_(
        FreedSlot.clinic_id == clinic_id,
        FreedSlot.dentist_user_id == dentist_user_id,
        FreedSlot.closed_at.is_(None),
        # La cota de abajo no cambia el resultado (un aviso que empieza más de 8 h antes no llega
        # a pisar), pero acota el índice: sin ella se recorría toda la historia abierta de la
        # clínica en cada alta, cambio de horario o de estado.
        FreedSlot.start_time_utc >= start - DURACION_MAXIMA_TURNO,
        FreedSlot.start_time_utc < end,
        FreedSlot.end_time_utc > start,
    )).all()


def _cerrar_aviso(aviso: FreedSlot, motivo: FreedSlotCloseReason, user_id: int, ahora: datetime) -> None:
    # Un mismo pedido puede encontrar dos veces el mismo aviso (el horario viejo y el nuevo de un
    # turno movido lo pisan los dos): el primer cierre es el que vale.
    if aviso.closed_at is not None:
        return
    aviso.closed_at = ahora
    aviso.closed_by_user_id = user_id
    aviso.close_reason = motivo


def registrar_hueco_liberado(db: Session, *, clinic_id: int, dentist_user_id: int,
                             start: datetime, end: datetime, source_appointment_id: int,
                             reason: FreedSlotReason, user_id: int,
                             ahora: Optional[datetime] = None) -> Optional[FreedSlot]:
    """
    Anota que un horario quedó libre. Un turno que ya empezó no deja nada que ofrecer.

    Los avisos abiertos del mismo odontólogo que se pisan con este horario se cierran como
    SUPERSEDED: un turno cancelado, reactivado y vuelto a cancelar no avisa dos veces.
    """
    ahora = ahora or ahora_utc()
    if start <= ahora:
        return None
    for viejo in _avisos_abiertos_que_pisan(db, clinic_id, dentist_user_id, start, end):
        _cerrar_aviso(viejo, FreedSlotCloseReason.SUPERSEDED, user_id, ahora)
    hueco = FreedSlot(
        clinic_id=clinic_id,
        dentist_user_id=dentist_user_id,
        start_time_utc=start,
        end_time_utc=end,
        source_appointment_id=source_appointment_id,
        reason=reason,
        created_by_user_id=user_id,
        created_at=ahora,
    )
    db.add(hueco)
    return hueco


def cerrar_avisos_ocupados(db: Session, *, clinic_id: int, dentist_user_id: int,
                           start: datetime, end: datetime, user_id: int,
                           ahora: Optional[datetime] = None) -> None:
    """
    El horario está ocupado (turno nuevo, movido o reactivado): sus avisos se cierran.

    Sin esto el aviso quedaba abierto y escondido mientras el turno lo pisara, y **reaparecía**
    si ese turno después se borraba — o sea que borrar avisaba, cuando la regla es que no. Por la
    misma razón se llama también al borrar un turno activo: un aviso que otra transacción registró
    mientras éste esperaba su lock no lo vio nadie cerrar.
    """
    ahora = ahora or ahora_utc()
    for aviso in _avisos_abiertos_que_pisan(db, clinic_id, dentist_user_id, start, end):
        _cerrar_aviso(aviso, FreedSlotCloseReason.FILLED, user_id, ahora)


def cerrar_avisos_del_turno_borrado(db: Session, *, clinic_id: int, appointment_id: int, user_id: int,
                                    ahora: Optional[datetime] = None) -> None:
    """
    Se borró un turno: los avisos que había dejado se cierran.

    Borrar es «lo cargué mal»: ese turno nunca existió, así que cancelarlo o moverlo antes no
    liberó ningún horario de verdad. Sin esto, el aviso de un turno cancelado y después borrado
    seguía ofreciendo un hueco que nadie había dejado.
    """
    ahora = ahora or ahora_utc()
    abiertos = db.query(FreedSlot).filter(and_(
        FreedSlot.clinic_id == clinic_id,
        FreedSlot.source_appointment_id == appointment_id,
        FreedSlot.closed_at.is_(None),
    )).all()
    for aviso in abiertos:
        _cerrar_aviso(aviso, FreedSlotCloseReason.DELETED, user_id, ahora)


# ---------------------------------------------------------------------------
# La lista
# ---------------------------------------------------------------------------

def _query_entradas_esperando(db: Session, *, clinic_id: int, roles: list[str], user_id: int):
    query = db.query(WaitlistEntry).filter(and_(
        WaitlistEntry.clinic_id == clinic_id,
        WaitlistEntry.status == WaitlistStatus.WAITING,
    ))
    if solo_lo_suyo(roles):
        query = query.filter(WaitlistEntry.dentist_user_id == user_id)
    return query


def entradas_esperando(db: Session, *, clinic_id: int, roles: list[str], user_id: int) -> list[WaitlistEntry]:
    """
    Quienes esperan, en orden de llegada. El `entry_id` desempata a los anotados en el mismo
    segundo: sin él, MySQL puede devolver empatados en cualquier orden (lección del #288).
    """
    return (
        _query_entradas_esperando(db, clinic_id=clinic_id, roles=roles, user_id=user_id)
        .order_by(WaitlistEntry.created_at.asc(), WaitlistEntry.entry_id.asc())
        .all()
    )


def contar_esperando(db: Session, *, clinic_id: int, roles: list[str], user_id: int) -> int:
    return _query_entradas_esperando(db, clinic_id=clinic_id, roles=roles, user_id=user_id).count()


def _turnos_futuros_por_paciente(db: Session, clinic_id: int, pacientes: set[int],
                                 ahora: datetime) -> dict[int, list[Appointment]]:
    """
    Los turnos activos que todavía no terminaron de cada paciente, del más próximo al más lejano.
    Incluye el que está en curso: no cuenta como próximo, pero sí como choque de horario.
    """
    if not pacientes:
        return {}
    turnos = (
        _turnos_activos(db, clinic_id)
        .filter(and_(Appointment.patient_user_id.in_(sorted(pacientes)), Appointment.end_time_utc > ahora))
        .order_by(Appointment.start_time_utc.asc(), Appointment.appointment_id.asc())
        .all()
    )
    por_paciente: dict[int, list[Appointment]] = defaultdict(list)
    for turno in turnos:
        por_paciente[turno.patient_user_id].append(turno)
    return por_paciente


def _proximo_turno(entrada: WaitlistEntry, turnos: list[Appointment], ahora: datetime) -> Optional[Appointment]:
    """
    El próximo turno del paciente que cuenta para esta entrada: con ese odontólogo, o con
    cualquiera si espera a cualquiera. Uno que ya empezó no se puede adelantar.
    """
    for turno in turnos:
        if turno.start_time_utc <= ahora:
            continue
        if entrada.dentist_user_id is None or turno.dentist_user_id == entrada.dentist_user_id:
            return turno
    return None


def proximos_turnos(db: Session, *, clinic_id: int, entradas: list[WaitlistEntry],
                    ahora: Optional[datetime] = None) -> dict[int, Optional[Appointment]]:
    """El próximo turno de cada entrada, por `entry_id`. Una sola consulta para toda la lista."""
    ahora = ahora or ahora_utc()
    por_paciente = _turnos_futuros_por_paciente(db, clinic_id, {e.patient_user_id for e in entradas}, ahora)
    return {e.entry_id: _proximo_turno(e, por_paciente.get(e.patient_user_id, []), ahora) for e in entradas}


@dataclass
class HuecoConCandidatos:
    aviso: FreedSlot
    # (entrada, turno que se le adelantaría o None), sólo los que puede ver quien pide.
    candidatos: list
    # Si quien pide puede descartarlo. Un odontólogo no descarta un aviso que también tiene
    # candidatos de la recepción: se lo cerraría a ella sin que se entere.
    puede_descartar: bool


def armar_query_avisos(db: Session, *, clinic_id: int, roles: list[str], user_id: int,
                       ahora: datetime, slot_id: Optional[int] = None):
    """
    Los avisos que el cartel puede mostrar, filtrados EN LA BASE: abiertos, que no empezaron,
    que siguen libres y con al menos una entrada que en principio lo quiera.

    ⚠️ Antes esto se calculaba en Python: se traían todos los avisos abiertos y todos los turnos
    entre el primero y el último, y se comparaban de a pares. Un solo aviso a un año traía la
    agenda entera del año (17.000 turnos, medido), y como los avisos sin candidatos no los ve
    nadie, tampoco los descarta nadie: mover un turno mil veces alcanzaba para frenar el servicio.
    """
    ocupado = exists().where(and_(
        Appointment.clinic_id == FreedSlot.clinic_id,
        Appointment.dentist_user_id == FreedSlot.dentist_user_id,
        Appointment.status.in_(list(ACTIVE_STATUSES)),
        Appointment.deleted_at.is_(None),
        Appointment.start_time_utc < FreedSlot.end_time_utc,
        Appointment.end_time_utc > FreedSlot.start_time_utc,
    ))
    alguien_lo_quiere = exists().where(and_(
        WaitlistEntry.clinic_id == FreedSlot.clinic_id,
        WaitlistEntry.status == WaitlistStatus.WAITING,
        or_(WaitlistEntry.dentist_user_id.is_(None), WaitlistEntry.dentist_user_id == FreedSlot.dentist_user_id),
        WaitlistEntry.available_from_utc <= FreedSlot.start_time_utc,
        WaitlistEntry.dentist_user_id == user_id if solo_lo_suyo(roles) else true(),
    ))
    query = db.query(FreedSlot).filter(and_(
        FreedSlot.clinic_id == clinic_id,
        FreedSlot.closed_at.is_(None),
        FreedSlot.start_time_utc > ahora,
        ~ocupado,
        alguien_lo_quiere,
    ))
    if solo_lo_suyo(roles):
        query = query.filter(FreedSlot.dentist_user_id == user_id)
    if slot_id is not None:
        query = query.filter(FreedSlot.slot_id == slot_id)
    # El `slot_id` desempata dos avisos a la misma hora (dos odontólogos): ver `entradas_esperando`.
    return query.order_by(FreedSlot.start_time_utc.asc(), FreedSlot.slot_id.asc())


def huecos_con_candidatos(db: Session, *, clinic_id: int, roles: list[str], user_id: int,
                          ahora: Optional[datetime] = None, slot_id: Optional[int] = None,
                          limite: Optional[int] = None) -> list[HuecoConCandidatos]:
    """
    Los huecos que el cartel tiene que mostrar, cada uno con quién lo quiere.

    Un candidato es una entrada que espera a ese odontólogo o a cualquiera, desde un día que no
    es posterior al hueco, cuyo paciente no es el que lo liberó ni tiene ya un turno que lo pise,
    y que **no tiene un turno anterior al hueco**: si ya tiene uno antes, no lo quiere. Si tiene
    uno posterior, viaja con él: es el que se adelanta.

    Los candidatos se calculan contra la lista de TODA la clínica y después se filtra lo que ve
    quien pide: así se sabe si un odontólogo puede descartar el aviso sin quitárselo a la
    recepción.
    """
    ahora = ahora or ahora_utc()
    limite = LIMITE_AVISOS if limite is None else limite
    avisos = (
        armar_query_avisos(db, clinic_id=clinic_id, roles=roles, user_id=user_id, ahora=ahora, slot_id=slot_id)
        .limit(limite)
        .all()
    )
    if not avisos:
        return []

    # La lista entera de la clínica, con los permisos de la recepción: ver el docstring.
    entradas = entradas_esperando(db, clinic_id=clinic_id, roles=["RECEPTIONIST"], user_id=user_id)
    por_paciente = _turnos_futuros_por_paciente(db, clinic_id, {e.patient_user_id for e in entradas}, ahora)

    # El paciente de cada turno que liberó un hueco: a él no se le ofrece su propio horario.
    paciente_que_libero = dict(
        db.query(Appointment.appointment_id, Appointment.patient_user_id)
        .filter(and_(
            Appointment.clinic_id == clinic_id,
            Appointment.appointment_id.in_(sorted({a.source_appointment_id for a in avisos})),
        ))
        .all()
    )
    propio = solo_lo_suyo(roles)

    resultado = []
    for aviso in avisos:
        todos = []
        for entrada in entradas:
            if entrada.dentist_user_id is not None and entrada.dentist_user_id != aviso.dentist_user_id:
                continue
            if entrada.available_from_utc > aviso.start_time_utc:
                continue
            if entrada.patient_user_id == paciente_que_libero.get(aviso.source_appointment_id):
                continue
            turnos = por_paciente.get(entrada.patient_user_id, [])
            if any(_se_pisan(t.start_time_utc, t.end_time_utc, aviso.start_time_utc, aviso.end_time_utc)
                   for t in turnos):
                continue
            proximo = _proximo_turno(entrada, turnos, ahora)
            if proximo is not None and proximo.start_time_utc <= aviso.start_time_utc:
                continue
            todos.append((entrada, proximo))

        visibles = [c for c in todos if not propio or c[0].dentist_user_id == user_id]
        if visibles:
            resultado.append(HuecoConCandidatos(
                aviso=aviso,
                candidatos=visibles,
                puede_descartar=len(visibles) == len(todos),
            ))
    return resultado


# ---------------------------------------------------------------------------
# Darle el turno a alguien de la lista
# ---------------------------------------------------------------------------

def armar_query_entrada(db: Optional[Session], entry_id: int, clinic_id: int, con_lock: bool):
    """
    La consulta de la entrada que se va a resolver, separada para poder probar el lock sin base.

    Con `FOR UPDATE`, dos recepcionistas que le dan un turno a la misma persona a la vez no
    terminan con dos turnos: la segunda espera, encuentra la entrada ya resuelta y recibe un 422.
    El lock sólo corre en MySQL/MariaDB y los tests corren en SQLite: el test compila esta
    consulta contra MySQL y exige que esté (lección del #294).
    """
    sesion = db if db is not None else Session()
    query = sesion.query(WaitlistEntry).filter(and_(
        WaitlistEntry.entry_id == entry_id,
        WaitlistEntry.clinic_id == clinic_id,
    ))
    if con_lock:
        query = query.with_for_update()
    return query


def bloquear_entrada(db: Session, *, entry_id: int, clinic_id: int, con_lock: bool) -> Optional[WaitlistEntry]:
    """
    Toma el lock de la entrada sin decidir nada todavía. Para adelantar un turno, que tiene que
    pedir la entrada ANTES que el turno (ver el orden de los locks arriba) pero validarla después
    de saber de qué paciente es el turno.
    """
    return armar_query_entrada(db, entry_id, clinic_id, con_lock).first()


def validar_entrada(entrada: Optional[WaitlistEntry], *, patient_user_id: int,
                    roles: list[str], user_id: int) -> WaitlistEntry:
    """
    La entrada a la que se le va a dar el turno, si todavía se puede.

    Un solo 422 para todos los casos —no existe, es de otra clínica, ya se resolvió, es de otro
    paciente, es de la lista de otro odontólogo— para no contarle a nadie qué hay en una lista
    que no puede ver. Lo normal es que otra persona le haya dado el turno primero.
    """
    disponible = (
        entrada is not None
        and entrada.status == WaitlistStatus.WAITING
        and entrada.patient_user_id == patient_user_id
        and (not solo_lo_suyo(roles) or entrada.dentist_user_id == user_id)
    )
    if not disponible:
        raise HTTPException(status_code=422, detail="Waiting list entry is not available")
    return entrada


def tomar_entrada(db: Session, *, entry_id: int, clinic_id: int, patient_user_id: int,
                  roles: list[str], user_id: int, con_lock: bool) -> WaitlistEntry:
    """Lock y validación juntos: el camino del turno nuevo, que ya sabe de qué paciente es."""
    entrada = bloquear_entrada(db, entry_id=entry_id, clinic_id=clinic_id, con_lock=con_lock)
    return validar_entrada(entrada, patient_user_id=patient_user_id, roles=roles, user_id=user_id)


def resolver_entrada(entrada: WaitlistEntry, *, appointment_id: int, user_id: int,
                     ahora: Optional[datetime] = None) -> None:
    ahora = ahora or ahora_utc()
    entrada.status = WaitlistStatus.BOOKED
    entrada.appointment_id = appointment_id
    entrada.closed_at = ahora
    entrada.closed_by_user_id = user_id


def actualizar_si_espera(db: Session, *, entry_id: int, clinic_id: int, roles: list[str],
                         user_id: int, cambios: dict) -> bool:
    """
    Cambia una entrada SÓLO si todavía espera, en un único UPDATE condicional. Devuelve si tocó
    alguna fila.

    ⚠️ Leer la entrada y después guardarla dejaba una carrera: si mientras tanto otra persona le
    daba el turno, «Sacar de la lista» pisaba el BOOKED con un REMOVED y la entrada quedaba
    sacada con un turno anotado. Con la condición en el UPDATE, la base decide sobre la fila tal
    como está en ese momento, y no depende del dialecto: se prueba igual en SQLite.
    """
    query = _query_entradas_esperando(db, clinic_id=clinic_id, roles=roles, user_id=user_id)
    filas = query.filter(WaitlistEntry.entry_id == entry_id).update(
        {**cambios, WaitlistEntry.updated_at: ahora_utc()}, synchronize_session=False,
    )
    return filas > 0
