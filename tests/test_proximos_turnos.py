"""
Próximos turnos de varios pacientes (#355): `POST /v1/appointments/upcoming-by-patient`.

Ortodoncia le pregunta a este servicio, de un grupo de pacientes, quién ya tiene turno, para no
ofrecerle uno de control. Lo que fija este archivo:

1. **Quién entra**: el personal de la clínica. El paciente no, y un rol que el servicio no conoce
   tampoco.
2. **Qué devuelve**: una entrada por paciente pedido, con su próximo turno con cada odontólogo, y de
   cada turno sólo cuándo es y con quién. Ni el motivo ni las observaciones.
3. **Qué es «tener turno»**: uno que ocupa un hueco y todavía no terminó. Ni cancelado, ni ausente,
   ni atendido, ni borrado.
4. **El alcance**: la clínica de quien pregunta, y dentro de ella el turno cuenta aunque sea con
   otro odontólogo.
5. **Una sola consulta** para todo el grupo, y con la forma que le deja usar el índice a MySQL. Los
   tests corren en SQLite, que no tiene ese índice: por eso se mira la consulta y la migración.

Los casos con más de un usuario usan `_como(...)` y no dos fixtures de cliente: las dependencias de
FastAPI se reemplazan en la app entera.
"""
import contextlib
import functools
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

import lista_espera
from conftest import _make_client, _sembrar_csrf, engine, override_get_db
from dependencies import get_current_user, get_db
from main import app
from models import Appointment, AppointmentStatus
from test_agenda_recepcion import _db
from test_integridad_turnos import RAIZ, _sql_de_las_migraciones

P = "/clinic-scheduling-api/v1/appointments/upcoming-by-patient"
RECEPCION = (50, ["RECEPTIONIST"])

# Escritos a mano y no importados del servicio: si allá se cambiaran, acá se tiene que notar.
ESTADOS_QUE_OCUPAN_EL_HUECO = {"SCHEDULED", "CONFIRMED", "ARRIVED"}
DATOS_DE_UN_TURNO = {"appointment_id", "dentist_user_id", "start_time_utc"}
INDICE = "ix_appointments_clinic_patient_start"
COLUMNAS_DEL_INDICE = ["clinic_id", "patient_user_id", "start_time_utc"]
MIGRACION, LA_ANTERIOR = "a7c4e9d2b6f3", "f2a6c8d4e1b9"
TOPE_DE_ESPERA = "SET SESSION lock_wait_timeout = 10"


@functools.lru_cache(maxsize=None)
def _sql_del_upgrade():
    """El SQL de `alembic upgrade head`, pedido una sola vez (arma un proceso aparte)."""
    return _sql_de_las_migraciones()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ahora():
    """Ahora, en UTC naive como lo guarda la base."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _turno(paciente, en=timedelta(days=1), minutos=30, dentist=1, estado=AppointmentStatus.SCHEDULED,
           clinica=1, borrado=False, inicio=None, **extra):
    """
    Turno directo en la base, que empieza `en` desde ahora (negativo: ya empezó). Devuelve su id.
    Con `inicio` se le da el horario exacto: para dos turnos a la misma hora no alcanza con pedir
    el mismo `en`, porque cada llamada mira el reloj por su cuenta.
    """
    db = _db()
    inicio = inicio or (_ahora() + en).replace(microsecond=0)
    apt = Appointment(
        clinic_id=clinica, dentist_user_id=dentist, patient_user_id=paciente,
        patient_name=f"Paciente {paciente}", start_time_utc=inicio,
        end_time_utc=inicio + timedelta(minutes=minutos), status=estado,
        deleted_at=_ahora() if borrado else None, **extra,
    )
    db.add(apt)
    db.commit()
    apt_id = apt.appointment_id
    db.close()
    return apt_id


def _inicio(apt_id):
    db = _db()
    try:
        return db.get(Appointment, apt_id).start_time_utc
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


def _pedir(cliente, ids):
    return cliente.post(P, json={"patient_ids": ids})


def _por_paciente(res):
    """La respuesta como {paciente: [id de turno, ...]}, respetando el orden en que vino."""
    assert res.status_code == 200, res.text
    return {p["patient_user_id"]: [t["appointment_id"] for t in p["next_by_dentist"]] for p in res.json()["patients"]}


# ---------------------------------------------------------------------------
# 1. Quién entra
# ---------------------------------------------------------------------------

def test_sin_sesion_es_401_y_no_403():
    """401 es lo que hace que el front mande a iniciar sesión; un 403 dejaba la pantalla trabada."""
    _turno(5)
    anteriores = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: None
    try:
        with TestClient(app) as cliente:
            _sembrar_csrf(cliente)  # con el token puesto: el rechazo tiene que ser por la sesión
            assert _pedir(cliente, [5]).status_code == 401
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(anteriores)


@pytest.mark.parametrize("roles", [["RECEPTIONIST"], ["DENTIST"], ["ADMIN"]])
def test_entra_el_personal_de_la_clinica(roles):
    turno = _turno(5)
    with _como(70, roles) as cliente:
        assert _por_paciente(_pedir(cliente, [5])) == {5: [turno]}


@pytest.mark.parametrize("roles", [["PATIENT"], ["OTRO_ROL"], []])
def test_el_paciente_y_un_rol_desconocido_no_entran(roles):
    """
    El paciente pregunta por sí mismo (id 10) y aun así no entra: esto es para el personal. Y un
    rol que este servicio no conoce falla cerrado.
    """
    _turno(10)
    with _como(10, roles) as cliente:
        res = _pedir(cliente, [10])
    assert res.status_code == 403, res.text
    assert "next_by_dentist" not in res.text


def test_sin_el_token_csrf_no_entra(client_sin_reraise):
    """Es un POST: aunque sólo lea, pasa por la misma puerta que los que escriben."""
    _turno(5)
    res = _pedir(client_sin_reraise, [5])
    assert res.status_code == 403
    assert res.json() == {"detail": "CSRF token validation failed"}


def test_el_instrumento_ese_mismo_cliente_con_el_token_entra(client_sin_reraise):
    turno = _turno(5)
    _sembrar_csrf(client_sin_reraise)
    assert _por_paciente(_pedir(client_sin_reraise, [5])) == {5: [turno]}


def test_solo_se_pide_por_post(receptionist_client):
    """
    Por GET no hay nada: un GET no pasa por el control de CSRF, y además esa dirección la atiende
    `GET /{appointment_id}`, que no la entiende.
    """
    _turno(5)
    res = receptionist_client.request("GET", P, json={"patient_ids": [5]})
    assert res.status_code in (405, 422), res.text
    assert "patients" not in res.text


def test_tiene_el_tope_de_pedidos_de_las_lecturas_vecinas(receptionist_client):
    """60 por minuto y por usuario: una pantalla con 2.000 pacientes hace diez pedidos, no cien."""
    respuestas = [_pedir(receptionist_client, [5]).status_code for _ in range(61)]
    assert respuestas[:60] == [200] * 60
    assert respuestas[60] == 429


# ---------------------------------------------------------------------------
# 2. Qué devuelve
# ---------------------------------------------------------------------------

def test_una_entrada_por_paciente_en_el_orden_pedido(receptionist_client):
    del_5 = _turno(5, en=timedelta(days=3), dentist=1)
    primero_del_7 = _turno(7, en=timedelta(days=1), dentist=2)
    segundo_del_7 = _turno(7, en=timedelta(days=9), dentist=1)

    res = _pedir(receptionist_client, [7, 5, 6])

    assert res.status_code == 200, res.text
    cuerpo = res.json()
    assert set(cuerpo.keys()) == {"patients"}
    assert [p["patient_user_id"] for p in cuerpo["patients"]] == [7, 5, 6]
    assert all(set(p.keys()) == {"patient_user_id", "next_by_dentist"} for p in cuerpo["patients"])
    del_7, el_5, el_6 = cuerpo["patients"]
    # El que no tiene turno viene igual, con la lista vacía: «no tiene» se dice, no se deduce.
    assert el_6["next_by_dentist"] == []
    assert [t["appointment_id"] for t in del_7["next_by_dentist"]] == [primero_del_7, segundo_del_7]
    assert [t["dentist_user_id"] for t in del_7["next_by_dentist"]] == [2, 1]
    [turno] = el_5["next_by_dentist"]
    assert set(turno.keys()) == DATOS_DE_UN_TURNO
    assert turno["appointment_id"] == del_5
    assert turno["dentist_user_id"] == 1
    # UTC sin huso, como el resto del servicio. Si un día sale con «Z», que se note acá: los
    # fronts la leen con `toUtc`, que acepta las dos formas, pero el contrato lo tiene que decir.
    assert turno["start_time_utc"] == _inicio(del_5).isoformat()


def test_no_trae_nada_mas_que_cuando_y_con_quien(receptionist_client):
    """
    Esta respuesta la ve también quien no es el odontólogo del turno: ni el motivo, ni las
    observaciones, ni los datos del paciente guardados en el turno.
    """
    _turno(5, reason="SECRETO motivo de la consulta", observations="SECRETO observaciones",
           patient_phone="SECRETO-351-555", patient_email="secreto@example.com",
           patient_dni="SECRETO-DNI", patient_address="SECRETO domicilio")

    res = _pedir(receptionist_client, [5])

    assert res.status_code == 200, res.text
    [paciente] = res.json()["patients"]
    [turno] = paciente["next_by_dentist"]  # el instrumento: el turno sí está
    assert set(turno.keys()) == DATOS_DE_UN_TURNO
    assert "SECRETO" not in res.text and "secreto" not in res.text
    assert "Paciente 5" not in res.text


def test_el_proximo_turno_con_cada_odontologo(receptionist_client):
    """
    El caso por el que no se corta en «los tres primeros»: cuatro turnos con otro odontólogo y
    recién el quinto con el suyo. Viene el primero de cada uno, y el del suyo no se pierde.
    """
    # Cargados desordenados a propósito: el orden lo da el horario, no el id.
    _turno(5, en=timedelta(days=5), dentist=1)
    primero_con_el_1 = _turno(5, en=timedelta(days=1), dentist=1)
    _turno(5, en=timedelta(days=20), dentist=1)
    _turno(5, en=timedelta(days=3), dentist=1)
    con_el_2 = _turno(5, en=timedelta(days=9), dentist=2)
    _turno(5, en=timedelta(days=7), dentist=1)
    _turno(5, en=timedelta(days=30), dentist=2)
    # Otro paciente con muchos turnos no le saca lugar al primero.
    for dia in range(1, 6):
        _turno(6, en=timedelta(days=dia), dentist=3)

    res = _pedir(receptionist_client, [5, 6])

    respuesta = _por_paciente(res)
    assert respuesta[5] == [primero_con_el_1, con_el_2]
    assert [t["dentist_user_id"] for t in res.json()["patients"][0]["next_by_dentist"]] == [1, 2]
    assert len(respuesta[6]) == 1


def test_el_primero_de_la_lista_es_el_mas_proximo_de_todos(receptionist_client):
    """Para «¿tiene algún turno, y cuándo?» alcanza con mirar el primero, sea con quien sea."""
    con_el_1 = _turno(5, en=timedelta(days=4), dentist=1)
    con_el_2 = _turno(5, en=timedelta(days=2), dentist=2)
    con_el_3 = _turno(5, en=timedelta(days=6), dentist=3)

    assert _por_paciente(_pedir(receptionist_client, [5])) == {5: [con_el_2, con_el_1, con_el_3]}


def test_dos_turnos_a_la_misma_hora_salen_siempre_en_el_mismo_orden(receptionist_client):
    """Con el mismo horario desempata el id: sin eso MySQL puede devolverlos en cualquier orden."""
    a_esa_hora = (_ahora() + timedelta(days=2)).replace(microsecond=0)
    con_el_2 = _turno(5, inicio=a_esa_hora, dentist=2)
    con_el_1 = _turno(5, inicio=a_esa_hora, dentist=1)

    assert _por_paciente(_pedir(receptionist_client, [5])) == {5: [con_el_2, con_el_1]}


def test_los_repetidos_cuentan_una_vez(receptionist_client):
    turno = _turno(5)
    assert _por_paciente(_pedir(receptionist_client, [5, 5, 6, 5])) == {5: [turno], 6: []}
    assert [p["patient_user_id"] for p in _pedir(receptionist_client, [6, 5, 6]).json()["patients"]] == [6, 5]


def test_una_lista_vacia_devuelve_una_respuesta_vacia(receptionist_client):
    _turno(5)
    res = _pedir(receptionist_client, [])
    assert res.status_code == 200, res.text
    assert res.json() == {"patients": []}


def test_hasta_200_pacientes_por_pedido(receptionist_client):
    turno = _turno(200)

    de_a_200 = _pedir(receptionist_client, list(range(1, 201)))
    assert de_a_200.status_code == 200, de_a_200.text
    assert len(de_a_200.json()["patients"]) == 200
    assert _por_paciente(de_a_200)[200] == [turno]

    assert _pedir(receptionist_client, list(range(1, 202))).status_code == 422
    # El tope es sobre lo que se manda, repetidos incluidos: no hay que contar para saber si entra.
    assert _pedir(receptionist_client, [5] * 201).status_code == 422


@pytest.mark.parametrize("cuerpo", [
    {"patient_ids": [0]},
    {"patient_ids": [5, -1]},
    {"patient_ids": ["cinco"]},
    {"patient_ids": ["5"]},        # un número escrito como texto
    {"patient_ids": [5.0]},
    {"patient_ids": [5.5]},
    {"patient_ids": [True]},       # sin esto, `true` se toma por el paciente 1
    {"patient_ids": [None]},
    {"patient_ids": [2**63]},      # no entra en un BIGINT: sin tope llega hasta la base
    {"patient_ids": [10**30]},
    {"patient_ids": 5},
    {"patient_ids": None},
    {},
    {"pacientes": [5]},
    # Una clave de más se rechaza: un filtro que no existe, ignorado, devolvería todo sin filtrar.
    {"patient_ids": [5], "dentist_user_id": 1},
    {"patient_ids": [5], "patient_id": 5},
])
def test_un_pedido_mal_armado_es_422(client_sin_reraise, cuerpo):
    """Con el cliente que no re-lanza: un 500 del servidor tiene que verse como 500, no como excepción."""
    _turno(5)
    _turno(1)
    _sembrar_csrf(client_sin_reraise)
    res = client_sin_reraise.post(P, json=cuerpo)
    assert res.status_code == 422, res.text
    assert "next_by_dentist" not in res.text


def test_el_id_mas_grande_que_existe_se_acepta(receptionist_client):
    """El borde del tope: lo más grande que entra en la columna. No tiene turnos, pero no es un error."""
    assert _por_paciente(_pedir(receptionist_client, [2**63 - 1])) == {2**63 - 1: []}


# ---------------------------------------------------------------------------
# 3. Qué es «tener turno»
# ---------------------------------------------------------------------------

def test_cuentan_solo_los_estados_que_ocupan_el_hueco(receptionist_client):
    """Un paciente por estado: tiene turno el programado, el confirmado y el que está en la sala."""
    turnos = {}
    for paciente, estado in enumerate(AppointmentStatus, start=20):
        turnos[paciente] = (estado.value, _turno(paciente, estado=estado))

    respuesta = _por_paciente(_pedir(receptionist_client, list(turnos)))

    assert len(turnos) == 6, "hay un estado nuevo: decidí si cuenta como «tener turno» y anotalo acá"
    for paciente, (estado, turno) in turnos.items():
        esperado = [turno] if estado in ESTADOS_QUE_OCUPAN_EL_HUECO else []
        assert respuesta[paciente] == esperado, f"un turno {estado}"


def test_un_turno_borrado_no_cuenta(receptionist_client):
    _turno(5, borrado=True)
    vivo = _turno(6)
    assert _por_paciente(_pedir(receptionist_client, [5, 6])) == {5: [], 6: [vivo]}


def test_el_que_ya_termino_no_cuenta_y_el_que_esta_en_curso_si(receptionist_client):
    _turno(5, en=timedelta(minutes=-31), minutos=30)            # terminó hace un minuto
    _turno(5, en=timedelta(days=-3))                            # el de la semana pasada
    en_curso = _turno(6, en=timedelta(minutes=-10), minutos=30)  # lo están atendiendo
    empieza_ya = _turno(7, en=timedelta(minutes=1))

    assert _por_paciente(_pedir(receptionist_client, [5, 6, 7])) == {5: [], 6: [en_curso], 7: [empieza_ya]}


def test_un_turno_de_8_horas_que_todavia_no_termino_cuenta(receptionist_client):
    """
    El control de la cota que acota el índice: «empezó hace menos de 8 horas». Un turno de las 8
    horas máximas que empezó hace 7 h 59 todavía no terminó, y tiene que estar.
    """
    largo = _turno(5, en=-timedelta(hours=7, minutes=59), minutos=8 * 60)
    _turno(6, en=-timedelta(hours=9), minutos=8 * 60)  # el mismo turno, una hora antes: ya terminó

    assert _por_paciente(_pedir(receptionist_client, [5, 6])) == {5: [largo], 6: []}


def test_ahora_es_el_reloj_del_servicio(receptionist_client, monkeypatch):
    """
    «Todavía no terminó» se mide contra el reloj en UTC del servicio (`_utcnow_naive`), no contra la
    hora de la máquina. Se adelanta ese reloj diez días: así el test no depende del huso de quien lo
    corre (con la hora local en vez de UTC, en un servidor que ya está en UTC no se notaría nada).
    """
    import routers.appointments as ra
    _turno(5, en=timedelta(days=5), dentist=1)
    dentro_de_quince = _turno(5, en=timedelta(days=15), dentist=2)
    monkeypatch.setattr(ra, "_utcnow_naive", lambda: _ahora() + timedelta(days=10))

    assert _por_paciente(_pedir(receptionist_client, [5])) == {5: [dentro_de_quince]}


# ---------------------------------------------------------------------------
# 4. El alcance
# ---------------------------------------------------------------------------

def test_un_odontologo_ve_tambien_el_turno_con_un_colega():
    """
    «No tiene turno» tiene que ser cierto aunque el turno sea con otro odontólogo de la clínica:
    la pregunta es por el paciente. Por eso la respuesta dice con quién es cada uno.
    """
    con_el_colega = _turno(5, dentist=1)
    propio = _turno(5, en=timedelta(days=4), dentist=99)

    with _como(99, ["DENTIST"]) as odontologo:
        res = _pedir(odontologo, [5])

    assert _por_paciente(res) == {5: [con_el_colega, propio]}
    assert [t["dentist_user_id"] for t in res.json()["patients"][0]["next_by_dentist"]] == [1, 99]


def test_cada_clinica_ve_lo_suyo():
    """El mismo id de paciente con turnos en las dos clínicas: cada una ve el suyo y nada del otro."""
    en_la_1 = _turno(5, clinica=1, dentist=1)
    en_la_2 = _turno(5, clinica=2, dentist=8, en=timedelta(days=2))
    solo_en_la_2 = _turno(6, clinica=2, dentist=8)

    with _como(*RECEPCION, clinic_id=1) as recepcion:
        assert _por_paciente(_pedir(recepcion, [5, 6])) == {5: [en_la_1], 6: []}
    with _como(60, ["RECEPTIONIST"], clinic_id=2) as la_otra:
        assert _por_paciente(_pedir(la_otra, [5, 6])) == {5: [en_la_2], 6: [solo_en_la_2]}


# ---------------------------------------------------------------------------
# 5. Una sola consulta, y con la forma que usa el índice
# ---------------------------------------------------------------------------

def test_es_una_sola_consulta_para_todos_los_pacientes(receptionist_client):
    for paciente in range(1, 41):
        _turno(paciente)
        _turno(paciente, en=timedelta(days=2), dentist=2)
    consultas = []

    def anotar(conexion, cursor, sentencia, parametros, contexto, muchas):
        if "appointments" in sentencia.lower():
            consultas.append(sentencia)

    event.listen(engine, "before_cursor_execute", anotar)
    try:
        res = _pedir(receptionist_client, list(range(1, 41)))
    finally:
        event.remove(engine, "before_cursor_execute", anotar)

    assert all(len(turnos) == 2 for turnos in _por_paciente(res).values())
    assert len(consultas) == 1, f"se esperaba una consulta a los turnos y hubo {len(consultas)}"


def _consulta_en_mysql(ahora, pacientes=frozenset({5, 6}), clinica=1):
    from sqlalchemy.dialects import mysql
    consulta = lista_espera.armar_query_turnos_futuros(None, clinica, set(pacientes), ahora)
    return " ".join(str(consulta.statement.compile(
        dialect=mysql.dialect(), compile_kwargs={"literal_binds": True},
    )).split())


def test_la_consulta_filtra_por_las_tres_columnas_del_indice_y_ordena():
    """
    Compilada contra MySQL, que es donde corre: la clínica, la lista de pacientes y la cota sobre
    el inicio, con el valor exacto (8 horas antes de ahora). Con 7 h 59 quedaría afuera un turno
    que todavía no terminó; sin la cota, MySQL lee todo el historial de cada paciente.

    ⚠️ El orden se mira acá y no sólo en las respuestas: el modelo declara el índice, SQLite lo usa
    y devuelve los turnos ordenados por horario aunque nadie se lo pida. Los tests que miran el
    orden de una respuesta pasan igual sin el `ORDER BY`; en MySQL, no.
    """
    ahora = datetime(2030, 6, 15, 14, 0)
    sql = _consulta_en_mysql(ahora)

    assert "appointments.clinic_id = 1" in sql
    assert "appointments.patient_user_id IN (5, 6)" in sql
    assert "appointments.start_time_utc > '2030-06-15 06:00:00'" in sql
    assert "appointments.end_time_utc > '2030-06-15 14:00:00'" in sql
    assert "appointments.deleted_at IS NULL" in sql
    for estado in ESTADOS_QUE_OCUPAN_EL_HUECO:
        assert f"'{estado}'" in sql
    for estado in ("CANCELLED", "NO_SHOW", "COMPLETED"):
        assert f"'{estado}'" not in sql
    assert sql.endswith("ORDER BY appointments.start_time_utc ASC, appointments.appointment_id ASC")


def test_el_modelo_declara_el_indice_con_las_columnas_en_ese_orden():
    indices = {i.name: [c.name for c in i.columns] for i in Appointment.__table__.indexes}
    assert indices.get(INDICE) == COLUMNAS_DEL_INDICE


def test_las_migraciones_crean_ese_mismo_indice():
    """
    Los tests arman la base desde el modelo, así que el índice del modelo no prueba que exista en
    producción: lo crea una migración. Se mira el SQL que corre `alembic upgrade head`.
    """
    que_lo_nombran = [s for s in _sql_del_upgrade() if INDICE in s]
    assert que_lo_nombran == [
        f"CREATE INDEX {INDICE} ON appointments ({', '.join(COLUMNAS_DEL_INDICE)})"
    ], que_lo_nombran


def test_la_migracion_no_espera_para_siempre_a_que_le_dejen_la_tabla():
    """
    El deploy corre la migración con el servicio atendiendo. Crear el índice necesita la tabla para
    sí un instante: si hay una transacción abierta que la leyó, MySQL espera, y mientras espera
    frena a todas las consultas nuevas a los turnos. De fábrica esa espera no tiene tope.

    El tope va justo ANTES del `CREATE INDEX` (después no sirve de nada), y son 10 segundos: lo
    que la agenda puede quedar quieta, como mucho, antes de que el deploy falle sin cambiar nada.
    """
    sentencias = list(_sql_del_upgrade())
    crear = next(i for i, s in enumerate(sentencias) if s.startswith(f"CREATE INDEX {INDICE} "))
    assert sentencias[crear - 1] == TOPE_DE_ESPERA, sentencias[crear - 1:crear + 1]


def test_volver_atras_saca_ese_indice_y_nada_mas():
    """
    El SQL del `downgrade` de esta migración, sin base (como el del `upgrade`). Un downgrade que
    borrara otra cosa no lo nota nadie hasta el día que hace falta. Sacar el índice también
    necesita la tabla para sí: lleva el mismo tope de espera, y antes.
    """
    salida = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", f"{MIGRACION}:{LA_ANTERIOR}", "--sql"],
        cwd=RAIZ, capture_output=True, encoding="utf-8", timeout=120,
        env={**os.environ, "DATABASE_URL": "mysql+pymysql://offline@localhost/offline", "PYTHONUTF8": "1"},
    )
    assert salida.returncode == 0, salida.stderr
    lineas = [linea for linea in salida.stdout.splitlines() if not linea.startswith("--")]
    sentencias = [" ".join(s.split()) for s in " ".join(lineas).split(";") if s.strip()]
    sin_la_version = [s for s in sentencias if "alembic_version" not in s]

    assert sin_la_version == [TOPE_DE_ESPERA, f"DROP INDEX {INDICE} ON appointments"], sentencias
    assert any(LA_ANTERIOR in s for s in sentencias if "alembic_version" in s), sentencias


def test_las_migraciones_tienen_una_sola_punta():
    """Dos migraciones que salen de la misma hacen fallar el `alembic upgrade head` del deploy."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    configuracion = Config(str(RAIZ / "alembic.ini"))
    # `script_location` es relativo a donde se corre: así el test no depende de eso.
    configuracion.set_main_option("script_location", str(RAIZ / "alembic"))
    assert len(ScriptDirectory.from_config(configuracion).get_heads()) == 1


# ---------------------------------------------------------------------------
# 6. La cuenta que comparte con la lista de espera
# ---------------------------------------------------------------------------

def test_la_cuenta_compartida_trae_solo_a_los_pacientes_pedidos_que_tienen_turno():
    """
    `turnos_futuros_por_paciente` la usan este endpoint y la lista de espera. Trae los turnos de
    los pacientes que se le piden y de ninguno más (desde el endpoint no se nota: se queda sólo con
    los que pidió). Un paciente sin turnos no está en el resultado, y sin pacientes no va a la base.
    """
    turno = _turno(5)
    _turno(7)  # tiene turno, pero nadie preguntó por él
    db = _db()
    try:
        resultado = lista_espera.turnos_futuros_por_paciente(db, 1, {5, 6}, _ahora())
        assert {p: [t.appointment_id for t in ts] for p, ts in resultado.items()} == {5: [turno]}
        assert lista_espera.turnos_futuros_por_paciente(None, 1, set(), _ahora()) == {}
    finally:
        db.close()
