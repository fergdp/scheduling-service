"""
Lista de espera (#298): anotar, los avisos de hueco liberado, a quién se le ofrece cada uno y
darle el turno — nuevo o adelantado — en el mismo commit que lo saca de la lista.

Los casos con más de un usuario usan `_como(...)` y no dos fixtures de cliente: las dependencias
de FastAPI se reemplazan en la app entera, así que dos fixtures activas a la vez hablan las dos
como el último usuario creado.
"""
import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import lista_espera
from conftest import _make_client, override_get_db
from dependencies import get_current_user, get_db
from main import app
from models import (
    Appointment, AppointmentStatus, FreedSlot, FreedSlotCloseReason, FreedSlotReason,
    WaitlistEntry, WaitlistStatus,
)
from test_agenda_recepcion import BASE, _db, _patch

W = "/clinic-scheduling-api/v1/waitlist"
RECEPCION = (50, ["RECEPTIONIST"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(dias, hora=15, minuto=0):
    """Un horario a `dias` de hoy, en UTC naive como lo guarda la base."""
    base = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=dias)
    return base.replace(hour=hora, minute=minuto, second=0, microsecond=0)


def _turno(inicio, minutos=30, dentist=1, paciente=5, estado=AppointmentStatus.SCHEDULED, clinica=1):
    """Turno directo en la base: no pasa por el endpoint, así que no cierra ni abre avisos."""
    db = _db()
    apt = Appointment(
        clinic_id=clinica, dentist_user_id=dentist, patient_user_id=paciente,
        patient_name=f"Paciente {paciente}", start_time_utc=inicio,
        end_time_utc=inicio + timedelta(minutes=minutos), status=estado,
    )
    db.add(apt)
    db.commit()
    apt_id = apt.appointment_id
    db.close()
    return apt_id


def _anotar(cliente, paciente, dentist=None, desde=None, nota=None):
    cuerpo = {
        "patient_user_id": paciente,
        "patient_name": f"Paciente {paciente}",
        "patient_phone": "11 5555-0000",
        "dentist_user_id": dentist,
        "available_from_utc": (desde or _utc(0, 0)).isoformat(),
    }
    if nota is not None:
        cuerpo["note"] = nota
    return cliente.post(f"{W}/", json=cuerpo)


def _id_anotado(cliente, paciente, **kw):
    res = _anotar(cliente, paciente, **kw)
    assert res.status_code == 200, res.text
    return res.json()["entry_id"]


def _fila(modelo, pk):
    db = _db()
    try:
        obj = db.get(modelo, pk)
        return {c.name: getattr(obj, c.name) for c in modelo.__table__.columns}
    finally:
        db.close()


def _avisos():
    db = _db()
    try:
        return [
            {c.name: getattr(a, c.name) for c in FreedSlot.__table__.columns}
            for a in db.query(FreedSlot).order_by(FreedSlot.slot_id).all()
        ]
    finally:
        db.close()


def _abiertos():
    return [a for a in _avisos() if a["closed_at"] is None]


def _turno_de(apt_id):
    return _fila(Appointment, apt_id)


def _turnos_del_paciente(paciente):
    db = _db()
    try:
        return db.query(Appointment).filter(Appointment.patient_user_id == paciente).count()
    finally:
        db.close()


@contextlib.contextmanager
def _como(user_id, roles, clinic_id=1):
    """Un cliente con ese usuario mientras dura el bloque; al salir vuelven las dependencias de antes."""
    generador = _make_client(lambda: {"user_id": user_id, "clinic_id": clinic_id, "roles": roles})
    cliente = next(generador)
    try:
        yield cliente
    finally:
        generador.close()


def _dar_turno(cliente, paciente, inicio, entry_id, dentist=1, minutos=30):
    return cliente.post(f"{BASE}/", json={
        "dentist_user_id": dentist, "patient_user_id": paciente,
        "start_time_utc": inicio.isoformat(),
        "end_time_utc": (inicio + timedelta(minutes=minutos)).isoformat(),
        "waitlist_entry_id": entry_id,
    })


# ---------------------------------------------------------------------------
# Anotar, ver, editar y sacar
# ---------------------------------------------------------------------------

def test_recepcion_anota_y_la_lista_lo_trae(receptionist_client):
    res = _anotar(receptionist_client, 6, nota="  sólo a la tarde  ")
    assert res.status_code == 200, res.text
    cuerpo = res.json()
    assert cuerpo["status"] == "WAITING"
    assert cuerpo["dentist_user_id"] is None
    assert cuerpo["note"] == "sólo a la tarde"
    assert cuerpo["next_appointment"] is None

    fila = _fila(WaitlistEntry, cuerpo["entry_id"])
    assert fila["created_by_user_id"] == 50
    assert fila["clinic_id"] == 1
    assert fila["patient_phone"] == "11 5555-0000"

    lista = receptionist_client.get(f"{W}/").json()
    assert lista["total"] == 1
    assert lista["entries"][0]["entry_id"] == cuerpo["entry_id"]


def test_una_nota_en_blanco_no_es_una_nota(receptionist_client):
    res = _anotar(receptionist_client, 6, nota="    ")
    assert res.status_code == 200
    assert res.json()["note"] is None


def test_la_lista_va_en_orden_de_llegada(receptionist_client):
    """Por `created_at`, y el id desempata a los anotados en el mismo segundo."""
    a = _id_anotado(receptionist_client, 6)
    b = _id_anotado(receptionist_client, 7)
    c = _id_anotado(receptionist_client, 8)
    db = _db()
    db.get(WaitlistEntry, c).created_at = datetime(2020, 1, 1)
    db.commit()
    db.close()
    ids = [e["entry_id"] for e in receptionist_client.get(f"{W}/").json()["entries"]]
    assert ids == [c, a, b]


def test_el_proximo_turno_respeta_el_odontologo_de_la_entrada(receptionist_client):
    con_otro = _turno(_utc(5), dentist=2, paciente=6)
    con_el = _turno(_utc(9), dentist=1, paciente=6)
    _turno(_utc(3), dentist=1, paciente=6, estado=AppointmentStatus.CANCELLED)   # no ocupa nada
    en_curso = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    _turno(en_curso, minutos=60, dentist=1, paciente=6)                           # ya empezó
    de_7_con_otro = _turno(_utc(4), dentist=2, paciente=7)
    _turno(_utc(8), dentist=1, paciente=7)

    para_el = _id_anotado(receptionist_client, 6, dentist=1)
    para_cualquiera = _id_anotado(receptionist_client, 7)

    entradas = {e["entry_id"]: e for e in receptionist_client.get(f"{W}/").json()["entries"]}
    assert entradas[para_el]["next_appointment"]["appointment_id"] == con_el
    assert entradas[para_cualquiera]["next_appointment"]["appointment_id"] == de_7_con_otro
    assert con_otro  # el turno con otro odontólogo no cuenta para la entrada del Dr. 1


def test_no_se_anota_dos_veces_lo_mismo(receptionist_client):
    """
    «Cualquier odontólogo» ya incluye a todos: choca con cualquier otra entrada del paciente, y
    una para un odontólogo choca con la de ese y con la de «cualquiera». Si no, el mismo paciente
    aparecía dos veces en el mismo aviso, con dos botones para darle el mismo hueco.
    """
    primera = _id_anotado(receptionist_client, 6, dentist=1)
    assert _anotar(receptionist_client, 6, dentist=1).status_code == 409
    assert _anotar(receptionist_client, 6).status_code == 409               # cualquiera ya incluye al 1
    assert _anotar(receptionist_client, 6, dentist=2).status_code == 200    # otro odontólogo sí

    assert _id_anotado(receptionist_client, 7)                              # cualquiera
    assert _anotar(receptionist_client, 7, dentist=1).status_code == 409    # ya espera a todos
    assert _anotar(receptionist_client, 7).status_code == 409

    # Fuera de la lista, se puede volver a anotar.
    assert receptionist_client.delete(f"{W}/{primera}").status_code == 204
    assert _anotar(receptionist_client, 6, dentist=1).status_code == 200


def test_validaciones_del_alta(receptionist_client, monkeypatch):
    import routers.appointments as ra
    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: {6: 1, 1: 1, 7: 2, 77: 2}.get(uid))

    assert _anotar(receptionist_client, 6, nota="x" * 301).status_code == 422
    assert _anotar(receptionist_client, 0).status_code == 422
    res = _anotar(receptionist_client, 7)
    assert res.status_code == 422 and "patient" in res.json()["detail"].lower()
    res = _anotar(receptionist_client, 6, dentist=77)
    assert res.status_code == 422 and "dentist" in res.json()["detail"].lower()
    sin_fecha = receptionist_client.post(f"{W}/", json={"patient_user_id": 6})
    assert sin_fecha.status_code == 422
    assert _anotar(receptionist_client, 6, dentist=1).status_code == 200


def test_editar_cambia_solo_lo_que_viene(receptionist_client):
    entrada = _id_anotado(receptionist_client, 6, dentist=1, nota="primera")
    desde_original = _fila(WaitlistEntry, entrada)["available_from_utc"]

    res = receptionist_client.patch(f"{W}/{entrada}", json={"note": "sólo martes"})
    assert res.status_code == 200
    fila = _fila(WaitlistEntry, entrada)
    assert (fila["note"], fila["dentist_user_id"], fila["available_from_utc"]) == ("sólo martes", 1, desde_original)

    assert receptionist_client.patch(f"{W}/{entrada}", json={"dentist_user_id": None}).status_code == 200
    assert _fila(WaitlistEntry, entrada)["dentist_user_id"] is None

    nuevo_desde = _utc(3, 0)
    assert receptionist_client.patch(
        f"{W}/{entrada}", json={"available_from_utc": nuevo_desde.isoformat()}).status_code == 200
    assert _fila(WaitlistEntry, entrada)["available_from_utc"] == nuevo_desde

    assert receptionist_client.patch(f"{W}/{entrada}", json={"available_from_utc": None}).status_code == 422
    assert receptionist_client.patch(f"{W}/{entrada}", json={"note": None}).status_code == 200
    assert _fila(WaitlistEntry, entrada)["note"] is None


def test_editar_a_un_odontologo_que_ya_espera_es_409(receptionist_client):
    _id_anotado(receptionist_client, 6, dentist=1)
    segunda = _id_anotado(receptionist_client, 6, dentist=2)
    assert receptionist_client.patch(f"{W}/{segunda}", json={"dentist_user_id": 1}).status_code == 409
    assert _fila(WaitlistEntry, segunda)["dentist_user_id"] == 2


def test_editar_con_un_odontologo_de_otra_clinica_es_422(receptionist_client, monkeypatch):
    import routers.appointments as ra
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: {1: 1, 6: 1, 77: 2}.get(uid))
    assert receptionist_client.patch(f"{W}/{entrada}", json={"dentist_user_id": 77}).status_code == 422
    assert _fila(WaitlistEntry, entrada)["dentist_user_id"] == 1


def test_sacar_de_la_lista(receptionist_client):
    entrada = _id_anotado(receptionist_client, 6)
    assert receptionist_client.delete(f"{W}/{entrada}").status_code == 204
    fila = _fila(WaitlistEntry, entrada)
    assert fila["status"] == WaitlistStatus.REMOVED
    assert fila["closed_by_user_id"] == 50 and fila["closed_at"] is not None
    # Ya no está: ni se saca otra vez ni se edita.
    assert receptionist_client.delete(f"{W}/{entrada}").status_code == 404
    assert receptionist_client.patch(f"{W}/{entrada}", json={"note": "x"}).status_code == 404
    assert receptionist_client.get(f"{W}/").json()["total"] == 0


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------

def test_el_paciente_no_entra_a_la_lista(patient_client):
    """Tiene `clinic_id` en su JWT: sin el guard de rol leería nombres y teléfonos de otros (#261)."""
    with _como(*RECEPCION) as recepcion:
        entrada = _id_anotado(recepcion, 6)
    assert patient_client.get(f"{W}/").status_code == 403
    assert _anotar(patient_client, 10).status_code == 403
    assert patient_client.get(f"{W}/slots").status_code == 403
    assert patient_client.post(f"{W}/slots/1/dismiss").status_code == 403
    assert patient_client.patch(f"{W}/{entrada}", json={"note": "x"}).status_code == 403
    assert patient_client.delete(f"{W}/{entrada}").status_code == 403
    assert _fila(WaitlistEntry, entrada)["status"] == WaitlistStatus.WAITING


def test_sin_sesion_es_401_y_no_403():
    """401 es lo que hace que el front mande a iniciar sesión; un 403 dejaba la pantalla trabada."""
    anteriores = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: None
    try:
        with TestClient(app) as cliente:
            assert cliente.get(f"{W}/").status_code == 401
            assert cliente.get(f"{W}/slots").status_code == 401
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(anteriores)


def test_otra_clinica_no_ve_ni_toca_nada():
    with _como(*RECEPCION) as recepcion:
        entrada = _id_anotado(recepcion, 6)
        apt = _turno(_utc(10), paciente=5)
        assert _patch(recepcion, apt, "CANCELLED").status_code == 200
    [aviso] = _avisos()

    with _como(60, ["RECEPTIONIST"], clinic_id=2) as ajena:
        assert ajena.get(f"{W}/").json() == {"entries": [], "total": 0}
        assert ajena.get(f"{W}/slots").json() == {"slots": [], "waiting_count": 0}
        assert ajena.patch(f"{W}/{entrada}", json={"note": "x"}).status_code == 404
        assert ajena.delete(f"{W}/{entrada}").status_code == 404
        assert ajena.post(f"{W}/slots/{aviso['slot_id']}/dismiss").status_code == 404

    assert _fila(WaitlistEntry, entrada)["status"] == WaitlistStatus.WAITING
    assert _abiertos() != []


def test_el_odontologo_maneja_solo_su_lista():
    with _como(*RECEPCION) as recepcion:
        de_otro = _id_anotado(recepcion, 6, dentist=1)
        _id_anotado(recepcion, 7)  # cualquiera: de la recepción

    with _como(99, ["DENTIST"]) as odontologo:
        assert _anotar(odontologo, 8).status_code == 403
        assert _anotar(odontologo, 8, dentist=1).status_code == 403
        suya = _id_anotado(odontologo, 8, dentist=99)

        lista = odontologo.get(f"{W}/").json()
        assert [e["entry_id"] for e in lista["entries"]] == [suya]

        assert odontologo.patch(f"{W}/{de_otro}", json={"note": "x"}).status_code == 404
        assert odontologo.delete(f"{W}/{de_otro}").status_code == 404
        assert odontologo.patch(f"{W}/{suya}", json={"dentist_user_id": None}).status_code == 403
        assert odontologo.patch(f"{W}/{suya}", json={"dentist_user_id": 1}).status_code == 403
        assert odontologo.patch(f"{W}/{suya}", json={"note": "sólo mañanas"}).status_code == 200

    assert _fila(WaitlistEntry, de_otro)["status"] == WaitlistStatus.WAITING


def test_solo_lo_suyo_falla_cerrado():
    """Un rol que el servicio no conoce queda acotado a lo suyo, no ve la clínica entera."""
    assert lista_espera.solo_lo_suyo(["DENTIST"]) is True
    assert lista_espera.solo_lo_suyo(["ROL_NUEVO"]) is True
    assert lista_espera.solo_lo_suyo([]) is True
    assert lista_espera.solo_lo_suyo(["RECEPTIONIST"]) is False
    assert lista_espera.solo_lo_suyo(["DENTIST", "ADMIN"]) is False


# ---------------------------------------------------------------------------
# Qué registra un hueco liberado
# ---------------------------------------------------------------------------

def test_cancelar_un_turno_futuro_registra_el_hueco(receptionist_client):
    inicio = _utc(10)
    apt = _turno(inicio)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [aviso] = _avisos()
    assert aviso["dentist_user_id"] == 1 and aviso["clinic_id"] == 1
    assert (aviso["start_time_utc"], aviso["end_time_utc"]) == (inicio, inicio + timedelta(minutes=30))
    assert aviso["reason"] == FreedSlotReason.CANCELLED
    assert aviso["source_appointment_id"] == apt
    assert aviso["created_by_user_id"] == 50
    assert aviso["closed_at"] is None


def test_un_turno_que_ya_empezo_no_deja_hueco(receptionist_client):
    empezado = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    apt = _turno(empezado, minutos=60)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert _avisos() == []


def test_el_paciente_que_cancela_desde_el_portal_tambien_avisa(patient_client):
    """Es el caso del que nadie se enteraba."""
    apt = _turno(_utc(10), paciente=10)
    assert _patch(patient_client, apt, "CANCELLED").status_code == 200
    [aviso] = _avisos()
    assert aviso["created_by_user_id"] == 10


def test_cancelar_un_turno_que_no_ocupaba_el_hueco_no_avisa(receptionist_client):
    apt = _turno(_utc(10), estado=AppointmentStatus.NO_SHOW)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert _avisos() == []


@pytest.mark.parametrize("estado", ["NO_SHOW", "COMPLETED"])
def test_ausente_y_atendido_no_avisan(receptionist_client, estado):
    apt = _turno(_utc(10))
    assert _patch(receptionist_client, apt, estado).status_code == 200
    assert _avisos() == []


def test_mover_a_otro_dia_registra_el_horario_viejo(receptionist_client):
    viejo = _utc(10)
    apt = _turno(viejo)
    res = receptionist_client.put(f"{BASE}/{apt}", json={"start_time_utc": _utc(12).isoformat()})
    assert res.status_code == 200, res.text
    [aviso] = _avisos()
    assert (aviso["start_time_utc"], aviso["end_time_utc"]) == (viejo, viejo + timedelta(minutes=30))
    assert aviso["reason"] == FreedSlotReason.RESCHEDULED
    assert aviso["closed_at"] is None


def test_correrlo_un_rato_o_estirarlo_no_libera_un_hueco(receptionist_client):
    inicio = _utc(10)
    apt = _turno(inicio, minutos=60)
    corrido = inicio + timedelta(minutes=15)
    assert receptionist_client.put(f"{BASE}/{apt}", json={"start_time_utc": corrido.isoformat()}).status_code == 200
    estirado = corrido + timedelta(minutes=90)
    assert receptionist_client.put(f"{BASE}/{apt}", json={"end_time_utc": estirado.isoformat()}).status_code == 200
    assert _avisos() == []


def test_pasarlo_a_otro_odontologo_libera_el_hueco_del_anterior(receptionist_client):
    inicio = _utc(10)
    apt = _turno(inicio, dentist=1)
    assert receptionist_client.put(f"{BASE}/{apt}", json={"dentist_user_id": 2}).status_code == 200
    [aviso] = _avisos()
    assert aviso["dentist_user_id"] == 1 and aviso["start_time_utc"] == inicio


def test_cambiar_el_motivo_o_mandar_el_mismo_horario_no_avisa(receptionist_client):
    inicio = _utc(10)
    apt = _turno(inicio)
    assert receptionist_client.put(f"{BASE}/{apt}", json={"reason": "control"}).status_code == 200
    assert receptionist_client.put(f"{BASE}/{apt}", json={"start_time_utc": inicio.isoformat()}).status_code == 200
    assert _avisos() == []


def test_borrar_no_avisa(receptionist_client):
    """Borrar es «lo cargué mal»: ese horario nunca estuvo ocupado de verdad."""
    apt = _turno(_utc(10))
    assert receptionist_client.delete(f"{BASE}/{apt}").status_code == 204
    assert _avisos() == []


def test_deshacer_la_cancelacion_cierra_el_aviso_y_recancelar_no_duplica(receptionist_client):
    apt = _turno(_utc(10))
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    [primero] = _abiertos()

    assert _patch(receptionist_client, apt, "SCHEDULED").status_code == 200
    assert _abiertos() == []
    assert _fila(FreedSlot, primero["slot_id"])["close_reason"] == FreedSlotCloseReason.FILLED

    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    [segundo] = _abiertos()
    assert segundo["slot_id"] != primero["slot_id"]


def test_un_hueco_que_se_vuelve_a_liberar_reemplaza_al_aviso_viejo(receptionist_client):
    inicio = _utc(10)
    primero = _turno(inicio)
    assert _patch(receptionist_client, primero, "CANCELLED").status_code == 200
    [viejo] = _abiertos()
    # Un turno que entra por la base, sin pasar por el endpoint: no cierra el aviso viejo.
    segundo = _turno(inicio, paciente=6)
    assert _patch(receptionist_client, segundo, "CANCELLED").status_code == 200

    [nuevo] = _abiertos()
    assert nuevo["source_appointment_id"] == segundo
    assert _fila(FreedSlot, viejo["slot_id"])["close_reason"] == FreedSlotCloseReason.SUPERSEDED


def test_dar_un_turno_en_el_hueco_cierra_el_aviso(receptionist_client):
    inicio = _utc(10)
    apt = _turno(inicio)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    [aviso] = _abiertos()

    res = receptionist_client.post(f"{BASE}/", json={
        "dentist_user_id": 1, "patient_user_id": 7,
        "start_time_utc": inicio.isoformat(), "end_time_utc": (inicio + timedelta(minutes=30)).isoformat(),
    })
    assert res.status_code == 200, res.text
    fila = _fila(FreedSlot, aviso["slot_id"])
    assert fila["close_reason"] == FreedSlotCloseReason.FILLED
    assert fila["closed_by_user_id"] == 50


def test_mover_otro_turno_al_hueco_cierra_el_aviso_y_libera_el_suyo(receptionist_client):
    inicio = _utc(10)
    cancelado = _turno(inicio)
    assert _patch(receptionist_client, cancelado, "CANCELLED").status_code == 200
    [aviso] = _abiertos()

    otro = _turno(_utc(12), paciente=6)
    assert receptionist_client.put(f"{BASE}/{otro}", json={"start_time_utc": inicio.isoformat()}).status_code == 200

    assert _fila(FreedSlot, aviso["slot_id"])["close_reason"] == FreedSlotCloseReason.FILLED
    [nuevo] = _abiertos()
    assert nuevo["start_time_utc"] == _utc(12) and nuevo["source_appointment_id"] == otro


def test_un_aviso_que_pisan_el_horario_viejo_y_el_nuevo_se_cierra_una_sola_vez(receptionist_client):
    """
    Un turno de tres horas cancelado deja un aviso largo. Uno de media hora que estaba adentro
    (entró por la base) se mueve a otra hora también adentro: el mismo pedido encuentra el
    aviso dos veces. Vale el primer cierre.
    """
    largo = _turno(_utc(10, 14), minutos=180)
    assert _patch(receptionist_client, largo, "CANCELLED").status_code == 200
    [aviso] = _abiertos()

    corto = _turno(_utc(10, 14), minutos=30, paciente=6)
    res = receptionist_client.put(f"{BASE}/{corto}", json={"start_time_utc": _utc(10, 16).isoformat()})
    assert res.status_code == 200, res.text
    assert _fila(FreedSlot, aviso["slot_id"])["close_reason"] == FreedSlotCloseReason.SUPERSEDED


# ---------------------------------------------------------------------------
# A quién se le ofrece cada hueco
# ---------------------------------------------------------------------------

def test_el_cartel_trae_el_hueco_con_quienes_lo_esperan_en_orden(receptionist_client):
    inicio = _utc(10)
    para_el = _id_anotado(receptionist_client, 6, dentist=1)
    para_cualquiera = _id_anotado(receptionist_client, 7)
    desde_ese_dia = _id_anotado(receptionist_client, 11, desde=inicio)
    _id_anotado(receptionist_client, 8, dentist=2)                          # espera a otro
    _id_anotado(receptionist_client, 9, desde=inicio + timedelta(days=1))   # le sirve desde después

    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    cuerpo = receptionist_client.get(f"{W}/slots").json()
    assert cuerpo["waiting_count"] == 5
    [hueco] = cuerpo["slots"]
    assert hueco["dentist_user_id"] == 1 and hueco["reason"] == "CANCELLED"
    assert [c["entry_id"] for c in hueco["candidates"]] == [para_el, para_cualquiera, desde_ese_dia]


def test_sin_nadie_que_lo_quiera_no_hay_cartel(receptionist_client):
    apt = _turno(_utc(10))
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert receptionist_client.get(f"{W}/slots").json() == {"slots": [], "waiting_count": 0}

    _id_anotado(receptionist_client, 6, dentist=2)
    assert receptionist_client.get(f"{W}/slots").json() == {"slots": [], "waiting_count": 1}


def test_al_que_libero_el_hueco_no_se_le_ofrece(receptionist_client):
    _id_anotado(receptionist_client, 5, dentist=1)
    apt = _turno(_utc(10), paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert receptionist_client.get(f"{W}/slots").json()["slots"] == []


def test_quien_ya_tiene_turno_antes_no_lo_quiere_y_quien_lo_tiene_despues_se_adelanta(receptionist_client):
    inicio = _utc(10)
    _turno(_utc(5), dentist=1, paciente=6)            # ya tiene uno antes
    despues = _turno(_utc(20), dentist=1, paciente=7)  # tiene uno después: se le adelanta
    _turno(_utc(3), dentist=2, paciente=8)             # tiene uno antes, pero con OTRO odontólogo
    _id_anotado(receptionist_client, 6, dentist=1)
    adelanta = _id_anotado(receptionist_client, 7, dentist=1)
    sin_turno_con_el = _id_anotado(receptionist_client, 8, dentist=1)

    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    candidatos = {c["entry_id"]: c for c in hueco["candidates"]}
    assert list(candidatos) == [adelanta, sin_turno_con_el]
    assert candidatos[adelanta]["next_appointment"]["appointment_id"] == despues
    assert candidatos[sin_turno_con_el]["next_appointment"] is None


def test_quien_tiene_un_turno_que_pisa_el_hueco_no_es_candidato(receptionist_client):
    inicio = _utc(10)
    _turno(inicio + timedelta(minutes=10), dentist=2, paciente=6)
    _id_anotado(receptionist_client, 6)
    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert receptionist_client.get(f"{W}/slots").json()["slots"] == []


def test_un_turno_mas_largo_que_el_hueco_se_adelanta_solo_si_entra(receptionist_client):
    """
    Adelantar conserva la duración: un turno de una hora no cabe en un hueco de media si la media
    hora siguiente está ocupada. Ofrecérselo terminaba en «horario ocupado» después de haberle
    escrito al paciente.
    """
    inicio = _utc(10)
    no_entra = _turno(_utc(20), minutos=60, dentist=1, paciente=6)
    _turno(_utc(21), minutos=30, dentist=1, paciente=7)
    no_entra_entrada = _id_anotado(receptionist_client, 6, dentist=1)
    entra = _id_anotado(receptionist_client, 7, dentist=1)
    sin_turno = _id_anotado(receptionist_client, 8, dentist=1)
    _turno(inicio + timedelta(minutes=30), dentist=1, paciente=9)   # la media hora siguiente

    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert [c["entry_id"] for c in hueco["candidates"]] == [entra, sin_turno]

    # La regla es la del cambio de horario: moverlo ahí da 409 de verdad.
    res = receptionist_client.put(f"{BASE}/{no_entra}", json={
        "start_time_utc": inicio.isoformat(), "waitlist_entry_id": no_entra_entrada,
    })
    assert res.status_code == 409


def test_lo_que_no_ocupa_el_horario_siguiente_no_le_impide_entrar(receptionist_client):
    """Un cancelado, un borrado, el turno de un colega o el de otra clínica a esa hora no tapan nada."""
    inicio = _utc(10)
    siguiente = inicio + timedelta(minutes=30)
    su_turno = _turno(_utc(20), minutos=60, dentist=1, paciente=6)
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    _turno(siguiente, dentist=1, paciente=9, estado=AppointmentStatus.CANCELLED)
    _turno(siguiente, dentist=2, paciente=9)
    _turno(siguiente, dentist=1, paciente=9, clinica=2)
    borrado = _turno(siguiente, dentist=1, paciente=9)
    db = _db()
    db.get(Appointment, borrado).deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.close()

    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    [candidato] = hueco["candidates"]
    assert candidato["entry_id"] == entrada
    assert candidato["next_appointment"]["appointment_id"] == su_turno

    res = receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": inicio.isoformat(), "waitlist_entry_id": entrada,
    })
    assert res.status_code == 200, res.text


def test_su_propio_turno_pegado_al_hueco_no_le_impide_entrar(receptionist_client):
    """El turno que se adelanta no choca consigo mismo: el cambio de horario también lo excluye."""
    inicio = _utc(10)
    su_turno = _turno(inicio + timedelta(minutes=30), minutos=60, dentist=1, paciente=6)
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert [c["next_appointment"]["appointment_id"] for c in hueco["candidates"]] == [su_turno]

    res = receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": inicio.isoformat(), "waitlist_entry_id": entrada,
    })
    assert res.status_code == 200, res.text


def test_no_se_le_adelanta_encima_de_otro_turno_suyo(receptionist_client):
    """
    Otro turno del mismo paciente, con otro odontólogo, en la media hora siguiente: el cambio de
    horario no lo frena (mira la agenda del odontólogo), pero el paciente quedaría en dos lados.
    """
    inicio = _utc(10)
    _turno(_utc(20), minutos=60, dentist=1, paciente=6)
    _turno(inicio + timedelta(minutes=30), dentist=2, paciente=6)
    _id_anotado(receptionist_client, 6, dentist=1)
    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert len(_abiertos()) == 1   # el aviso existe: lo que falta es a quién ofrecérselo
    assert receptionist_client.get(f"{W}/slots").json()["slots"] == []


def test_adelantar_a_otro_odontologo_mira_la_agenda_del_odontologo_del_hueco(receptionist_client):
    """
    Espera a cualquiera y su turno es con el odontólogo 2; el hueco es del 1. Adelantarlo lo pasa
    al 1, así que lo que tiene que estar libre después del hueco es la agenda del 1, no la del 2.
    """
    inicio = _utc(10)
    siguiente = inicio + timedelta(minutes=30)
    su_turno = _turno(_utc(20), minutos=60, dentist=2, paciente=6)
    entrada = _id_anotado(receptionist_client, 6)                  # cualquier odontólogo
    _turno(siguiente, dentist=2, paciente=9)                       # el 2 ocupado después: no importa
    apt = _turno(inicio, dentist=1, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert [c["next_appointment"]["appointment_id"] for c in hueco["candidates"]] == [su_turno]

    # Se ocupa el 1 a la media hora: ya no entra, y el cambio de horario lo confirma.
    _turno(siguiente, dentist=1, paciente=10)
    assert receptionist_client.get(f"{W}/slots").json()["slots"] == []
    res = receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": inicio.isoformat(), "dentist_user_id": 1, "waitlist_entry_id": entrada,
    })
    assert res.status_code == 409


def test_los_avisos_que_nadie_ve_no_tapan_a_uno_que_si_sirve(receptionist_client, monkeypatch):
    """
    El tope cuenta avisos que alguien ve. Con tope 1: el primero sólo lo «quiere» un paciente que ya
    tiene un turno antes (la consulta no lo sabe, se decide en Python), así que no lo ve nadie ni
    lo descarta nadie. No puede dejar afuera al segundo, que sí tiene a quién ofrecérselo.
    """
    monkeypatch.setattr(lista_espera, "LIMITE_AVISOS", 1)
    _turno(_utc(5), dentist=1, paciente=6)                          # 6 ya tiene uno antes de los dos
    _id_anotado(receptionist_client, 6, dentist=1)
    para_el_segundo = _id_anotado(receptionist_client, 7, dentist=1, desde=_utc(11, 0))
    primero = _turno(_utc(10), paciente=5)
    segundo = _turno(_utc(12), paciente=5)
    for apt in (primero, segundo):
        assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert hueco["start_time_utc"].startswith(_utc(12).isoformat()[:16])
    assert [c["entry_id"] for c in hueco["candidates"]] == [para_el_segundo]


def test_la_tanda_siguiente_no_saltea_un_aviso_a_la_misma_hora(receptionist_client, monkeypatch):
    """
    Dos avisos a la misma hora (dos odontólogos) caen en tandas distintas: la siguiente arranca
    después del último por hora Y por id. Sólo por hora, el segundo no aparecía nunca.
    """
    monkeypatch.setattr(lista_espera, "LIMITE_AVISOS", 1)
    inicio = _utc(10)
    _turno(_utc(5), dentist=1, paciente=6)                  # 6 ya tiene uno antes: no quiere el del 1
    _id_anotado(receptionist_client, 6, dentist=1)
    para_el_dos = _id_anotado(receptionist_client, 7, dentist=2)
    del_uno = _turno(inicio, dentist=1, paciente=5)
    del_dos = _turno(inicio, dentist=2, paciente=8)
    for apt in (del_uno, del_dos):                           # el del 1 queda con el id menor
        assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert hueco["dentist_user_id"] == 2
    assert [c["entry_id"] for c in hueco["candidates"]] == [para_el_dos]


def test_la_busqueda_de_avisos_visibles_tiene_cota(receptionist_client, monkeypatch):
    """Se miran como mucho TANDAS_MAXIMAS tandas: sin cota, cada recarga recorría todos los avisos."""
    monkeypatch.setattr(lista_espera, "LIMITE_AVISOS", 1)
    monkeypatch.setattr(lista_espera, "TANDAS_MAXIMAS", 1)
    _turno(_utc(5), dentist=1, paciente=6)
    _id_anotado(receptionist_client, 6, dentist=1)
    _id_anotado(receptionist_client, 7, dentist=1, desde=_utc(11, 0))
    for dia in (10, 12):
        apt = _turno(_utc(dia), paciente=5)
        assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert receptionist_client.get(f"{W}/slots").json()["slots"] == []

    monkeypatch.setattr(lista_espera, "TANDAS_MAXIMAS", 2)
    assert len(receptionist_client.get(f"{W}/slots").json()["slots"]) == 1


def test_un_hueco_ocupado_descartado_o_pasado_no_se_muestra(receptionist_client):
    _id_anotado(receptionist_client, 6)

    ocupado = _turno(_utc(10), paciente=5)
    descartado = _turno(_utc(11), paciente=5)
    visible = _turno(_utc(12), paciente=5)
    for apt in (ocupado, descartado, visible):
        assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    avisos = {a["source_appointment_id"]: a["slot_id"] for a in _avisos()}

    _turno(_utc(10), paciente=9)  # entra por la base: el aviso queda abierto pero pisado
    assert receptionist_client.post(f"{W}/slots/{avisos[descartado]}/dismiss").status_code == 204

    db = _db()
    pasado = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    db.add(FreedSlot(clinic_id=1, dentist_user_id=1, start_time_utc=pasado,
                     end_time_utc=pasado + timedelta(minutes=30), source_appointment_id=visible,
                     reason=FreedSlotReason.CANCELLED, created_by_user_id=50))
    db.commit()
    db.close()

    slots = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert [s["slot_id"] for s in slots] == [avisos[visible]]


def test_descartar_un_aviso():
    with _como(*RECEPCION) as recepcion:
        _id_anotado(recepcion, 6)
        suyo = _turno(_utc(10), paciente=5)
        otro = _turno(_utc(11), paciente=5)
        for apt in (suyo, otro):
            assert _patch(recepcion, apt, "CANCELLED").status_code == 200
        avisos = {a["source_appointment_id"]: a["slot_id"] for a in _avisos()}

        assert recepcion.post(f"{W}/slots/{avisos[suyo]}/dismiss").status_code == 204
        fila = _fila(FreedSlot, avisos[suyo])
        assert fila["close_reason"] == FreedSlotCloseReason.DISMISSED and fila["closed_by_user_id"] == 50
        assert recepcion.post(f"{W}/slots/{avisos[suyo]}/dismiss").status_code == 404

    with _como(60, ["RECEPTIONIST"], clinic_id=2) as ajena:
        assert ajena.post(f"{W}/slots/{avisos[otro]}/dismiss").status_code == 404
    with _como(99, ["DENTIST"]) as colega:
        assert colega.post(f"{W}/slots/{avisos[otro]}/dismiss").status_code == 404
    assert _fila(FreedSlot, avisos[otro])["closed_at"] is None


def test_el_odontologo_ve_solo_sus_huecos_y_sus_candidatos():
    with _como(*RECEPCION) as recepcion:
        del_uno = _id_anotado(recepcion, 6, dentist=1)
        cualquiera = _id_anotado(recepcion, 7)
        del_99 = _id_anotado(recepcion, 8, dentist=99)
        de_uno = _turno(_utc(10), dentist=1, paciente=5)
        de_99 = _turno(_utc(11), dentist=99, paciente=5)
        for apt in (de_uno, de_99):
            assert _patch(recepcion, apt, "CANCELLED").status_code == 200

        todos = recepcion.get(f"{W}/slots").json()["slots"]
        assert [[c["entry_id"] for c in s["candidates"]] for s in todos] == [[del_uno, cualquiera], [cualquiera, del_99]]

    with _como(99, ["DENTIST"]) as odontologo:
        cuerpo = odontologo.get(f"{W}/slots").json()
    assert cuerpo["waiting_count"] == 1
    [suyo] = cuerpo["slots"]
    assert suyo["dentist_user_id"] == 99
    assert [c["entry_id"] for c in suyo["candidates"]] == [del_99]


# ---------------------------------------------------------------------------
# Darle el hueco a alguien de la lista
# ---------------------------------------------------------------------------

def test_darle_el_turno_lo_saca_de_la_lista_en_el_mismo_pedido(receptionist_client):
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    inicio = _utc(10)
    apt = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    [aviso] = _abiertos()

    res = _dar_turno(receptionist_client, 6, inicio, entrada)
    assert res.status_code == 200, res.text
    nuevo = res.json()["appointment_id"]

    fila = _fila(WaitlistEntry, entrada)
    assert fila["status"] == WaitlistStatus.BOOKED
    assert fila["appointment_id"] == nuevo
    assert fila["closed_by_user_id"] == 50 and fila["closed_at"] is not None
    assert _fila(FreedSlot, aviso["slot_id"])["close_reason"] == FreedSlotCloseReason.FILLED
    assert receptionist_client.get(f"{W}/").json()["total"] == 0
    assert receptionist_client.get(f"{W}/slots").json()["slots"] == []


def test_una_entrada_que_ya_no_esta_disponible_no_crea_el_turno(monkeypatch):
    import routers.appointments as ra
    inicio = _utc(10)
    with _como(*RECEPCION) as recepcion:
        entrada = _id_anotado(recepcion, 6, dentist=1)
        sacada = _id_anotado(recepcion, 8, dentist=1)
        assert recepcion.delete(f"{W}/{sacada}").status_code == 204

        res = _dar_turno(recepcion, 7, inicio, entrada)   # la entrada es de otro paciente
        assert res.status_code == 422 and res.json()["detail"] == "Waiting list entry is not available"
        assert _dar_turno(recepcion, 8, inicio, sacada).status_code == 422
        assert _dar_turno(recepcion, 6, inicio, 9999).status_code == 422

    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: 2)
    with _como(60, ["RECEPTIONIST"], clinic_id=2) as ajena:
        assert _dar_turno(ajena, 6, inicio, entrada).status_code == 422
    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: 1)

    with _como(99, ["DENTIST"]) as colega:
        # Es su agenda, pero la entrada es de la lista del Dr. 1.
        assert _dar_turno(colega, 6, inicio, entrada, dentist=99).status_code == 422

    assert [_turnos_del_paciente(p) for p in (6, 7, 8)] == [0, 0, 0]
    assert _fila(WaitlistEntry, entrada)["status"] == WaitlistStatus.WAITING


def test_si_el_horario_esta_ocupado_la_entrada_sigue_esperando(receptionist_client):
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    inicio = _utc(10)
    _turno(inicio, paciente=5)
    assert _dar_turno(receptionist_client, 6, inicio, entrada).status_code == 409
    assert _fila(WaitlistEntry, entrada)["status"] == WaitlistStatus.WAITING
    assert _turnos_del_paciente(6) == 0


def test_adelantar_su_turno_lo_mueve_y_libera_el_viejo_para_el_siguiente(receptionist_client):
    adelanta = _id_anotado(receptionist_client, 6, dentist=1)
    siguiente = _id_anotado(receptionist_client, 7)
    su_turno = _turno(_utc(20), paciente=6, minutos=45)

    cancelado = _turno(_utc(10), paciente=5)
    assert _patch(receptionist_client, cancelado, "CANCELLED").status_code == 200
    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert hueco["candidates"][0]["next_appointment"]["appointment_id"] == su_turno

    res = receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": _utc(10).isoformat(), "waitlist_entry_id": adelanta,
    })
    assert res.status_code == 200, res.text
    movido = _turno_de(su_turno)
    assert movido["start_time_utc"] == _utc(10)
    assert movido["end_time_utc"] == _utc(10) + timedelta(minutes=45)  # conserva su duración

    fila = _fila(WaitlistEntry, adelanta)
    assert fila["status"] == WaitlistStatus.BOOKED and fila["appointment_id"] == su_turno
    assert _fila(FreedSlot, hueco["slot_id"])["close_reason"] == FreedSlotCloseReason.FILLED

    # La cadena: su horario viejo quedó libre y se le ofrece al siguiente.
    [nuevo] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert nuevo["start_time_utc"].startswith(_utc(20).isoformat()[:16])
    assert nuevo["reason"] == "RESCHEDULED"
    assert [c["entry_id"] for c in nuevo["candidates"]] == [siguiente]


def test_adelantar_sin_moverlo_es_422(receptionist_client):
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    inicio = _utc(20)
    su_turno = _turno(inicio, paciente=6)
    res = receptionist_client.put(f"{BASE}/{su_turno}", json={"reason": "x", "waitlist_entry_id": entrada})
    assert res.status_code == 422
    assert "moving" in res.json()["detail"]
    res = receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": inicio.isoformat(), "waitlist_entry_id": entrada,
    })
    assert res.status_code == 422
    assert _fila(WaitlistEntry, entrada)["status"] == WaitlistStatus.WAITING


def test_adelantar_con_la_entrada_de_otro_paciente_es_422_y_no_mueve_nada(receptionist_client):
    de_otro = _id_anotado(receptionist_client, 7, dentist=1)
    inicio = _utc(20)
    su_turno = _turno(inicio, paciente=6)
    res = receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": _utc(10).isoformat(), "waitlist_entry_id": de_otro,
    })
    assert res.status_code == 422
    assert _turno_de(su_turno)["start_time_utc"] == inicio
    assert _avisos() == []
    assert _fila(WaitlistEntry, de_otro)["status"] == WaitlistStatus.WAITING


# ---------------------------------------------------------------------------
# El lock de la entrada (sólo corre en MySQL/MariaDB)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("con_lock", [True, False])
def test_la_entrada_se_pide_con_for_update_cuando_corresponde(con_lock):
    from sqlalchemy.dialects import mysql as dialecto_mysql
    query = lista_espera.armar_query_entrada(None, entry_id=1, clinic_id=1, con_lock=con_lock)
    sql = str(query.statement.compile(dialect=dialecto_mysql.dialect())).upper()
    assert ("FOR UPDATE" in sql) is con_lock


@pytest.mark.parametrize("dialecto, esperado", [
    ("mysql", True), ("mariadb", True), ("sqlite", False),
])
def test_con_lock_segun_el_dialecto(dialecto, esperado):
    """`mariadb` va aparte: SQLAlchemy lo reporta con otro nombre y apagaba el lock en silencio."""
    import routers.appointments as ra
    db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name=dialecto)))
    assert ra._con_lock(db) is esperado
    assert ra._con_lock(SimpleNamespace()) is False


def test_dar_y_adelantar_piden_la_entrada_con_lock_en_produccion(receptionist_client, monkeypatch):
    """
    En SQLite `_con_lock` da False, así que un `con_lock=False` fijo en cualquier eslabón —el
    endpoint, `tomar_entrada`, `bloquear_entrada`— pasaría la suite entera. Se fuerza el dialecto
    de producción y se espía la ÚLTIMA pieza, la que arma la consulta: así se cubre la cadena
    completa y no sólo el primer llamado.
    """
    import routers.appointments as ra
    pedidos = []
    real = lista_espera.armar_query_entrada

    def espia(db, entry_id, clinic_id, con_lock):
        pedidos.append(con_lock)
        return real(db, entry_id, clinic_id, False)  # SQLite no soporta FOR UPDATE

    monkeypatch.setattr(ra, "_con_lock", lambda db: True)
    monkeypatch.setattr(lista_espera, "armar_query_entrada", espia)
    # El turno también se lee con lock en el PUT: en SQLite hay que apagarlo.
    real_turno = ra._armar_query_turno
    monkeypatch.setattr(ra, "_armar_query_turno", lambda db, i, c, con_lock: real_turno(db, i, c, False))

    nuevo = _id_anotado(receptionist_client, 6, dentist=1)
    assert _dar_turno(receptionist_client, 6, _utc(10), nuevo).status_code == 200
    adelanta = _id_anotado(receptionist_client, 7, dentist=1)
    su_turno = _turno(_utc(20), paciente=7)
    assert receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": _utc(11).isoformat(), "waitlist_entry_id": adelanta,
    }).status_code == 200
    assert pedidos == [True, True]


# ---------------------------------------------------------------------------
# Lo que destaparon las revisiones independientes
# ---------------------------------------------------------------------------

def test_el_desde_se_convierte_a_utc_y_no_se_recorta(receptionist_client):
    """
    Las 00:00 de Buenos Aires son las 03:00 UTC. Recortar el huso guardaba las 00:00 UTC: tres
    horas antes, y el paciente aparecía como candidato de huecos de la noche anterior.
    """
    res = receptionist_client.post(f"{W}/", json={
        "patient_user_id": 6, "available_from_utc": "2026-10-05T00:00:00-03:00",
    })
    assert res.status_code == 200, res.text
    entrada = res.json()["entry_id"]
    assert _fila(WaitlistEntry, entrada)["available_from_utc"] == datetime(2026, 10, 5, 3, 0)

    res = receptionist_client.patch(f"{W}/{entrada}", json={"available_from_utc": "2026-10-07T00:00:00-03:00"})
    assert res.status_code == 200, res.text
    assert _fila(WaitlistEntry, entrada)["available_from_utc"] == datetime(2026, 10, 7, 3, 0)


@pytest.mark.parametrize("fecha", [
    "9999-12-31T23:59:59-14:00",   # no entra en un datetime al pasarla a UTC
    "0001-01-01T00:00:00+14:00",
    "1999-12-31T00:00:00",         # antes del 2000
    "2999-01-01T00:00:00",         # más de dos años adelante
])
def test_una_fecha_absurda_es_422_y_no_500(receptionist_client, fecha):
    assert receptionist_client.post(f"{W}/", json={
        "patient_user_id": 6, "available_from_utc": fecha,
    }).status_code == 422
    entrada = _id_anotado(receptionist_client, 7)
    assert receptionist_client.patch(f"{W}/{entrada}", json={"available_from_utc": fecha}).status_code == 422


def test_un_turno_con_fecha_imposible_tambien_es_422(receptionist_client):
    """El mismo helper de husos lo usan los turnos: el arreglo los cubre a los dos."""
    res = receptionist_client.post(f"{BASE}/", json={
        "dentist_user_id": 1, "patient_user_id": 6, "start_time_utc": "9999-12-31T23:59:59-14:00",
    })
    assert res.status_code == 422


@pytest.mark.parametrize("campo, largo", [
    ("patient_phone", 50), ("patient_dni", 50), ("patient_timezone", 50),
    ("patient_name", 255), ("patient_email", 255), ("patient_address", 255),
])
def test_un_dato_del_paciente_mas_largo_que_su_columna_es_422(receptionist_client, campo, largo):
    """
    La ficha del paciente acepta, por ejemplo, un teléfono con dos números y una aclaración; la
    columna del turno tiene 50. En MySQL estricto eso era un 500 al darle el turno desde la lista
    de espera (SQLite no hace cumplir el largo: sin el tope en el esquema, acá daba 200).
    """
    inicio = _utc(10)
    cuerpo = {
        "dentist_user_id": 1, "patient_user_id": 6,
        "start_time_utc": inicio.isoformat(), "end_time_utc": (inicio + timedelta(minutes=30)).isoformat(),
    }
    valor = "a" * (largo - 6) + "@x.com" if campo == "patient_email" else "1" * largo
    assert receptionist_client.post(f"{BASE}/", json={**cuerpo, campo: valor}).status_code == 200

    otro = _utc(11)
    cuerpo.update(start_time_utc=otro.isoformat(), end_time_utc=(otro + timedelta(minutes=30)).isoformat())
    res = receptionist_client.post(f"{BASE}/", json={**cuerpo, campo: "a" + valor})
    assert res.status_code == 422
    assert f"at most {largo} characters" in res.text


@pytest.mark.parametrize("desde", ["SCHEDULED", "CONFIRMED", "ARRIVED"])
def test_cancelar_desde_cualquier_estado_activo_registra_el_hueco(receptionist_client, desde):
    """El portal cancela también desde CONFIRMED: probarlo sólo desde SCHEDULED no alcanzaba."""
    apt = _turno(_utc(10), estado=AppointmentStatus[desde])
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert len(_abiertos()) == 1


def test_pasarlo_a_otro_odontologo_cierra_los_avisos_del_nuevo(receptionist_client):
    """Cerrar los avisos del odontólogo VIEJO en vez del nuevo pasaba la suite: no había test."""
    inicio = _utc(10)
    del_dos = _turno(inicio, dentist=2)
    assert _patch(receptionist_client, del_dos, "CANCELLED").status_code == 200
    [aviso_del_dos] = _abiertos()

    del_uno = _turno(inicio, dentist=1, paciente=6)
    assert receptionist_client.put(f"{BASE}/{del_uno}", json={"dentist_user_id": 2}).status_code == 200

    assert _fila(FreedSlot, aviso_del_dos["slot_id"])["close_reason"] == FreedSlotCloseReason.FILLED
    [nuevo] = _abiertos()
    assert nuevo["dentist_user_id"] == 1


def test_el_turno_de_un_colega_a_la_misma_hora_no_tapa_el_hueco(receptionist_client):
    _id_anotado(receptionist_client, 6)
    inicio = _utc(10)
    _turno(inicio, dentist=2, paciente=9)
    apt = _turno(inicio, dentist=1, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert hueco["dentist_user_id"] == 1


def test_quien_esta_en_un_turno_que_pisa_el_hueco_no_es_candidato():
    """El turno EN CURSO del paciente cuenta como choque aunque no cuente como próximo."""
    ahora = datetime.now(timezone.utc).replace(tzinfo=None).replace(second=0, microsecond=0)
    with _como(*RECEPCION) as recepcion:
        _id_anotado(recepcion, 6, desde=ahora - timedelta(days=1))
        _turno(ahora - timedelta(minutes=10), minutos=90, dentist=2, paciente=6)   # en curso
        apt = _turno(ahora + timedelta(minutes=30), paciente=5)
        assert _patch(recepcion, apt, "CANCELLED").status_code == 200
        assert recepcion.get(f"{W}/slots").json()["slots"] == []


def test_otra_clinica_con_su_propia_lista_no_ve_los_huecos_ajenos(monkeypatch):
    """
    El caso que el test anterior no probaba: la clínica 2 TIENE gente esperando a cualquiera, así
    que la función no corta antes y el filtro por clínica de los avisos queda a la vista.
    """
    import routers.appointments as ra
    with _como(*RECEPCION) as recepcion:
        _id_anotado(recepcion, 6)
        apt = _turno(_utc(10), paciente=5)
        assert _patch(recepcion, apt, "CANCELLED").status_code == 200
        assert len(recepcion.get(f"{W}/slots").json()["slots"]) == 1

    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: 2)
    with _como(60, ["RECEPTIONIST"], clinic_id=2) as ajena:
        _id_anotado(ajena, 70)
        cuerpo = ajena.get(f"{W}/slots").json()
    assert cuerpo == {"slots": [], "waiting_count": 1}


def _sql_de(cliente, ruta, tabla):
    """Las consultas que arma un pedido, para mirar el ORDER BY (SQLite no desordena empatados)."""
    from sqlalchemy import event
    from conftest import engine
    consultas = []

    def _anotar_sql(conn, cursor, statement, parameters, context, executemany):
        consultas.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", _anotar_sql)
    try:
        assert cliente.get(ruta).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _anotar_sql)
    return [q for q in consultas if f"FROM {tabla}" in q and "ORDER BY" in q]


def test_la_lista_y_los_avisos_desempatan_por_id(receptionist_client):
    """Con filas empatadas MySQL puede devolverlas en cualquier orden (lección del #288)."""
    _id_anotado(receptionist_client, 6)
    apt = _turno(_utc(10), paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200

    [lista] = _sql_de(receptionist_client, f"{W}/", "waitlist_entries")
    assert "ORDER BY waitlist_entries.created_at ASC, waitlist_entries.entry_id ASC" in lista
    avisos = _sql_de(receptionist_client, f"{W}/slots", "freed_slots")
    assert any("ORDER BY freed_slots.start_time_utc ASC, freed_slots.slot_id ASC" in q for q in avisos), avisos


def test_el_cartel_tiene_tope(receptionist_client, monkeypatch):
    monkeypatch.setattr(lista_espera, "LIMITE_AVISOS", 2)
    _id_anotado(receptionist_client, 6)
    for dia in (12, 10, 11):
        apt = _turno(_utc(dia), paciente=5)
        assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    slots = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert [s["start_time_utc"][:10] for s in slots] == [_utc(10).isoformat()[:10], _utc(11).isoformat()[:10]]


def test_un_aviso_sin_nadie_que_lo_quiera_no_ocupa_lugar_en_el_tope(receptionist_client, monkeypatch):
    """Si no, cien turnos movidos sin nadie esperando tapaban el aviso que sí sirve."""
    monkeypatch.setattr(lista_espera, "LIMITE_AVISOS", 1)
    _id_anotado(receptionist_client, 6, dentist=2)
    sin_interes = _turno(_utc(10), dentist=1, paciente=5)
    con_interes = _turno(_utc(11), dentist=2, paciente=5)
    for apt in (sin_interes, con_interes):
        assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    assert hueco["dentist_user_id"] == 2


def test_el_odontologo_no_descarta_un_aviso_que_tambien_es_de_la_recepcion():
    with _como(*RECEPCION) as recepcion:
        _id_anotado(recepcion, 6, dentist=99)
        _id_anotado(recepcion, 7)                       # cualquiera: de la recepción
        compartido = _turno(_utc(10), dentist=99, paciente=5)
        assert _patch(recepcion, compartido, "CANCELLED").status_code == 200
        [aviso] = _abiertos()

    with _como(99, ["DENTIST"]) as odontologo:
        [suyo] = odontologo.get(f"{W}/slots").json()["slots"]
        assert suyo["can_dismiss"] is False
        assert [c["patient_user_id"] for c in suyo["candidates"]] == [6]
        assert odontologo.post(f"{W}/slots/{aviso['slot_id']}/dismiss").status_code == 403
    assert _fila(FreedSlot, aviso["slot_id"])["closed_at"] is None

    with _como(*RECEPCION) as recepcion:
        [visto] = recepcion.get(f"{W}/slots").json()["slots"]
        assert visto["can_dismiss"] is True
        assert [c["patient_user_id"] for c in visto["candidates"]] == [6, 7]


def test_el_odontologo_descarta_lo_que_es_solo_suyo_y_no_lo_que_no_ve():
    with _como(*RECEPCION) as recepcion:
        # Su paciente le sirve desde el día 8: el aviso del día 5 de su agenda no es para él.
        _id_anotado(recepcion, 6, dentist=99, desde=_utc(8, 0))
        suyo = _turno(_utc(10), dentist=99, paciente=5)
        # Ese aviso del día 5 lo quiere sólo la recepción: el odontólogo no lo ve, y no lo
        # descarta a ciegas adivinando el id.
        _id_anotado(recepcion, 7)
        ajeno = _turno(_utc(5), dentist=99, paciente=5)
        _id_anotado(recepcion, 8, dentist=1)
        for apt in (suyo, ajeno):
            assert _patch(recepcion, apt, "CANCELLED").status_code == 200
    avisos = {a["source_appointment_id"]: a["slot_id"] for a in _avisos()}

    with _como(99, ["DENTIST"]) as odontologo:
        slots = odontologo.get(f"{W}/slots").json()["slots"]
        assert [s["slot_id"] for s in slots] == [avisos[suyo]]
        assert slots[0]["can_dismiss"] is False     # la recepción también lo quiere (paciente 7)

    # Sin la entrada de «cualquiera», el aviso es sólo suyo y lo puede descartar.
    db = _db()
    db.query(WaitlistEntry).filter(WaitlistEntry.patient_user_id == 7).update(
        {WaitlistEntry.status: WaitlistStatus.REMOVED})
    db.commit()
    db.close()
    with _como(99, ["DENTIST"]) as odontologo:
        assert odontologo.post(f"{W}/slots/{avisos[ajeno]}/dismiss").status_code == 404
        assert odontologo.post(f"{W}/slots/{avisos[suyo]}/dismiss").status_code == 204
    assert _fila(FreedSlot, avisos[suyo])["close_reason"] == FreedSlotCloseReason.DISMISSED


def test_sacar_de_la_lista_no_pisa_un_turno_que_se_dio_mientras_tanto(receptionist_client, monkeypatch):
    """
    La carrera: «Sacar de la lista» leyó la entrada esperando y, antes de guardar, otra persona
    le dio el turno. El UPDATE condicional mira la fila como está en ese momento.
    """
    import routers.waitlist as rw
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    vieja = _fila(WaitlistEntry, entrada)
    monkeypatch.setattr(rw, "_cargar_entrada", lambda *a, **k: WaitlistEntry(**vieja))

    apt = _turno(_utc(10), paciente=6)
    db = _db()
    lista_espera.resolver_entrada(db.get(WaitlistEntry, entrada), appointment_id=apt, user_id=51)
    db.commit()
    db.close()

    assert receptionist_client.delete(f"{W}/{entrada}").status_code == 404
    assert receptionist_client.patch(f"{W}/{entrada}", json={"note": "x"}).status_code == 404
    fila = _fila(WaitlistEntry, entrada)
    assert (fila["status"], fila["appointment_id"], fila["note"]) == (WaitlistStatus.BOOKED, apt, None)


def test_editar_sin_cambios_no_toca_nada(receptionist_client):
    entrada = _id_anotado(receptionist_client, 6, dentist=1)
    antes = _fila(WaitlistEntry, entrada)
    res = receptionist_client.patch(f"{W}/{entrada}", json={"dentist_user_id": 1})
    assert res.status_code == 200
    assert _fila(WaitlistEntry, entrada) == antes


def test_borrar_un_turno_activo_cierra_los_avisos_que_tapaba(receptionist_client):
    """Un aviso que quedó abierto debajo de un turno no puede reaparecer al borrarlo."""
    _id_anotado(receptionist_client, 6)
    inicio = _utc(10)
    cancelado = _turno(inicio, paciente=5)
    assert _patch(receptionist_client, cancelado, "CANCELLED").status_code == 200
    [aviso] = _abiertos()
    tapa = _turno(inicio, paciente=9)   # entra por la base: el aviso sigue abierto debajo

    assert receptionist_client.delete(f"{BASE}/{tapa}").status_code == 204
    assert _fila(FreedSlot, aviso["slot_id"])["close_reason"] == FreedSlotCloseReason.FILLED
    assert receptionist_client.get(f"{W}/slots").json()["slots"] == []


def test_adelantar_hacia_otro_odontologo_cuando_esperaba_a_cualquiera(receptionist_client):
    cualquiera = _id_anotado(receptionist_client, 6)
    su_turno = _turno(_utc(20), dentist=1, paciente=6)
    cancelado = _turno(_utc(10), dentist=2, paciente=5)
    assert _patch(receptionist_client, cancelado, "CANCELLED").status_code == 200
    [aviso] = _abiertos()

    res = receptionist_client.put(f"{BASE}/{su_turno}", json={
        "start_time_utc": _utc(10).isoformat(), "dentist_user_id": 2, "waitlist_entry_id": cualquiera,
    })
    assert res.status_code == 200, res.text
    assert _turno_de(su_turno)["dentist_user_id"] == 2
    assert _fila(WaitlistEntry, cualquiera)["status"] == WaitlistStatus.BOOKED
    assert _fila(FreedSlot, aviso["slot_id"])["close_reason"] == FreedSlotCloseReason.FILLED
    [viejo] = _abiertos()
    assert (viejo["dentist_user_id"], viejo["start_time_utc"]) == (1, _utc(20))


def test_sin_sesion_dar_o_adelantar_un_turno_es_401():
    """Con el guard de rol adelante, la sesión vencida daba 403 y el diálogo quedaba trabado."""
    anteriores = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: None
    try:
        with TestClient(app) as cliente:
            from conftest import _sembrar_csrf
            _sembrar_csrf(cliente)
            assert cliente.post(f"{BASE}/", json={}).status_code == 401
            assert cliente.put(f"{BASE}/1", json={}).status_code == 401
            assert cliente.get(f"{BASE}/availability/dentist/1",
                               params={"start": "2030-01-01T00:00:00", "end": "2030-01-02T00:00:00"}).status_code == 401
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(anteriores)


@pytest.mark.parametrize("con_lock", [True, False])
def test_el_turno_se_pide_con_for_update_cuando_corresponde(con_lock):
    from sqlalchemy.dialects import mysql as dialecto_mysql
    import routers.appointments as ra
    query = ra._armar_query_turno(None, 1, 1, con_lock)
    sql = str(query.statement.compile(dialect=dialecto_mysql.dialect())).upper()
    assert ("FOR UPDATE" in sql) is con_lock


def test_editar_cambiar_el_estado_y_borrar_leen_el_turno_con_lock_en_produccion(receptionist_client, monkeypatch):
    """
    Sin lock, adelantar un turno mientras el paciente lo cancela desde el portal dejaba un turno
    CANCELADO movido al hueco. Se fuerza el dialecto de producción y se espía la consulta.
    """
    import routers.appointments as ra
    pedidos = []
    real = ra._armar_query_turno

    def espia(db, appointment_id, clinic_id, con_lock):
        pedidos.append(con_lock)
        return real(db, appointment_id, clinic_id, False)

    monkeypatch.setattr(ra, "_con_lock", lambda db: True)
    monkeypatch.setattr(ra, "_armar_query_turno", espia)
    # El solapamiento también pediría FOR UPDATE: en SQLite no se puede.
    monkeypatch.setattr(ra, "DIALECTOS_CON_LOCK", ())

    apt = _turno(_utc(10))
    assert receptionist_client.put(f"{BASE}/{apt}", json={"reason": "x"}).status_code == 200
    assert _patch(receptionist_client, apt, "CONFIRMED").status_code == 200
    assert receptionist_client.delete(f"{BASE}/{apt}").status_code == 204
    assert pedidos == [True, True, True]

    # Leer un turno no bloquea nada.
    otro = _turno(_utc(11))
    pedidos.clear()
    assert receptionist_client.get(f"{BASE}/{otro}").status_code == 200
    assert pedidos == [False]


def test_borrar_un_turno_cierra_los_avisos_que_habia_dejado_y_no_otros(receptionist_client):
    """
    Borrar es «lo cargué mal»: cancelar o mover ese turno antes no liberó nada de verdad. Los
    avisos de OTROS turnos siguen, aunque sean del mismo odontólogo.
    """
    _id_anotado(receptionist_client, 6)
    mal_cargado = _turno(_utc(10), paciente=5)
    de_verdad = _turno(_utc(11), paciente=7)
    movido = _turno(_utc(12), paciente=8)
    for apt in (mal_cargado, de_verdad):
        assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    assert receptionist_client.put(f"{BASE}/{movido}", json={"start_time_utc": _utc(14).isoformat()}).status_code == 200
    avisos = {a["source_appointment_id"]: a["slot_id"] for a in _avisos()}

    assert receptionist_client.delete(f"{BASE}/{mal_cargado}").status_code == 204
    assert receptionist_client.delete(f"{BASE}/{movido}").status_code == 204

    assert _fila(FreedSlot, avisos[mal_cargado])["close_reason"] == FreedSlotCloseReason.DELETED
    assert _fila(FreedSlot, avisos[movido])["close_reason"] == FreedSlotCloseReason.DELETED
    assert _fila(FreedSlot, avisos[de_verdad])["closed_at"] is None
    assert [s["slot_id"] for s in receptionist_client.get(f"{W}/slots").json()["slots"]] == [avisos[de_verdad]]
