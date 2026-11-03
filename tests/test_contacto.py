"""
A quién va el WhatsApp de un turno o de la lista de espera (issue #417).

Tres capas, como el arreglo:

- la regla (`a_quien_escribirle`, `es_menor`), caso por caso, la misma tabla que ortodoncia;
- la consulta (`datos_de_contacto`), contra tablas `users` y `patient_guardians` de verdad en un
  SQLite aparte: la clínica de cada lado, el tutor dado de baja, el orden;
- las rutas: que turnos y lista de espera devuelvan `contacto`.
"""
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import contacto
import dependencies
from test_agenda_recepcion import BASE, _insert
from test_lista_espera import W, _id_anotado

ANA = {"nombre": "Ana Gómez", "telefono": "351 555-1111"}
SIN_TEL = {"nombre": "Luis Gómez", "telefono": None}


# ---------------------------------------------------------------------------
# La regla
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("menor", [True, None], ids=["menor", "sin fecha de nacimiento"])
def test_a_cargo_de_alguien_y_menor_o_sin_fecha_se_le_escribe_al_tutor(menor):
    resultado = contacto.a_quien_escribirle("Valentina Gómez", "351 999-0000", [ANA], menor)
    assert resultado == {"es_tutor": True, "nombre": "Ana Gómez", "telefono": "351 555-1111"}


def test_el_tutor_sin_telefono_va_al_de_la_ficha_del_paciente():
    """El de la ficha es casi siempre el que dejó el adulto al anotarlo."""
    resultado = contacto.a_quien_escribirle("Valentina", "351 999-0000", [SIN_TEL], None)
    assert resultado == {"es_tutor": True, "nombre": "Luis Gómez", "telefono": "351 999-0000"}


def test_de_dos_tutores_el_primero_que_tiene_telefono():
    resultado = contacto.a_quien_escribirle("Valentina", "351 999-0000", [SIN_TEL, ANA], True)
    assert resultado["nombre"] == "Ana Gómez"
    assert resultado["telefono"] == "351 555-1111"


def test_de_dos_tutores_sin_telefono_el_primero():
    otro = {"nombre": "Otro", "telefono": "  "}
    resultado = contacto.a_quien_escribirle("Valentina", "351 999-0000", [SIN_TEL, otro], True)
    assert resultado == {"es_tutor": True, "nombre": "Luis Gómez", "telefono": "351 999-0000"}


def test_mayor_de_edad_con_telefono_se_le_escribe_a_el():
    resultado = contacto.a_quien_escribirle("Martín Gómez", "351 999-0000", [ANA], False)
    assert resultado == {"es_tutor": False, "nombre": "Martín Gómez", "telefono": "351 999-0000"}


def test_mayor_de_edad_sin_telefono_al_tutor_que_tenga():
    resultado = contacto.a_quien_escribirle("Martín Gómez", " ", [SIN_TEL, ANA], False)
    assert resultado == {"es_tutor": True, "nombre": "Ana Gómez", "telefono": "351 555-1111"}


def test_mayor_de_edad_sin_telefono_y_tutores_sin_telefono_a_el():
    resultado = contacto.a_quien_escribirle("Martín Gómez", None, [SIN_TEL], False)
    assert resultado == {"es_tutor": False, "nombre": "Martín Gómez", "telefono": None}


@pytest.mark.parametrize("menor", [True, False, None])
def test_sin_nadie_a_cargo_se_le_escribe_a_el(menor):
    resultado = contacto.a_quien_escribirle("Valentina", "351 999-0000", [], menor)
    assert resultado == {"es_tutor": False, "nombre": "Valentina", "telefono": "351 999-0000"}


@pytest.mark.parametrize("nacimiento, hoy, esperado", [
    (None, date(2026, 11, 2), None),
    (date(2008, 11, 3), date(2026, 11, 2), True),   # Cumple 18 mañana
    (date(2008, 11, 2), date(2026, 11, 2), False),  # Cumple 18 hoy
    (date(2000, 1, 1), date(2026, 11, 2), False),
    (date(2008, 2, 29), date(2026, 2, 27), True),   # 29 de febrero: cumple el 28, como `plusYears` en Java
    (date(2008, 2, 29), date(2026, 2, 28), False),
])
def test_es_menor(nacimiento, hoy, esperado):
    assert contacto.es_menor(nacimiento, hoy) is esperado


# ---------------------------------------------------------------------------
# La consulta, contra las tablas de verdad
# ---------------------------------------------------------------------------

@pytest.fixture
def base_de_la_clinica(monkeypatch):
    """Un SQLite con `users` y `patient_guardians`, con las columnas que lee la consulta."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as cx:
        cx.execute(text(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, clinic_id INTEGER, first_name TEXT, "
            "last_name TEXT, phone TEXT, date_of_birth DATE, deactivated_at DATETIME)"
        ))
        cx.execute(text(
            "CREATE TABLE patient_guardians (dependent_user_id INTEGER, guardian_user_id INTEGER, "
            "relationship TEXT, clinic_id INTEGER)"
        ))
    monkeypatch.setattr(dependencies, "ClinicSessionLocal", sessionmaker(bind=engine))

    def sql(sentencia, **datos):
        with engine.begin() as cx:
            cx.execute(text(sentencia), datos)
    return sql


def _usuario(sql, user_id, clinic_id=1, nombre="N", apellido="A", telefono=None, nacimiento=None, baja=None):
    sql(
        "INSERT INTO users VALUES (:id, :clinica, :nombre, :apellido, :tel, :nac, :baja)",
        id=user_id, clinica=clinic_id, nombre=nombre, apellido=apellido, tel=telefono, nac=nacimiento, baja=baja,
    )


def _vinculo(sql, paciente, tutor, clinic_id=1):
    sql("INSERT INTO patient_guardians VALUES (:p, :t, 'PARENT', :c)", p=paciente, t=tutor, c=clinic_id)


def test_trae_los_tutores_de_cada_paciente_en_orden(base_de_la_clinica):
    sql = base_de_la_clinica
    _usuario(sql, 10, nombre="Valentina")
    _usuario(sql, 22, nombre="Luis", apellido="Gómez")
    _usuario(sql, 21, nombre="Ana", apellido="Gómez", telefono="351 555-1111")
    _vinculo(sql, 10, 22)
    _vinculo(sql, 10, 21)

    datos = contacto.datos_de_contacto([10], 1)
    assert datos == {10: {"menor": None, "tutores": [
        {"nombre": "Ana Gómez", "telefono": "351 555-1111"},
        {"nombre": "Luis Gómez", "telefono": None},
    ]}}


def test_no_trae_tutores_de_otra_clinica_ni_dados_de_baja(base_de_la_clinica):
    sql = base_de_la_clinica
    _usuario(sql, 10)
    _usuario(sql, 21, clinic_id=2, telefono="111")              # El tutor es de otra clínica
    _usuario(sql, 22, telefono="222", baja="2026-10-10 10:00:00")  # Dado de baja
    _usuario(sql, 23, telefono="333")
    _vinculo(sql, 10, 21)
    _vinculo(sql, 10, 22)
    _vinculo(sql, 10, 23, clinic_id=2)                           # El vínculo es de otra clínica

    assert contacto.datos_de_contacto([10], 1) == {10: {"menor": None, "tutores": []}}


def test_el_control_un_tutor_valido_si_viene(base_de_la_clinica):
    """Sin esto el test de arriba pasaría con una consulta que no trae a nadie."""
    sql = base_de_la_clinica
    _usuario(sql, 10)
    _usuario(sql, 23, telefono="333")
    _vinculo(sql, 10, 23)

    assert contacto.datos_de_contacto([10], 1)[10]["tutores"] == [{"nombre": "N A", "telefono": "333"}]


def test_sabe_si_es_menor_por_la_fecha_de_nacimiento(base_de_la_clinica):
    sql = base_de_la_clinica
    _usuario(sql, 10, nacimiento="2020-05-01")
    _usuario(sql, 11, nacimiento="1990-05-01")
    _usuario(sql, 12)

    datos = contacto.datos_de_contacto([10, 11, 12], 1)
    assert (datos[10]["menor"], datos[11]["menor"], datos[12]["menor"]) == (True, False, None)


def test_un_tutor_de_otro_paciente_no_se_mezcla(base_de_la_clinica):
    sql = base_de_la_clinica
    _usuario(sql, 10)
    _usuario(sql, 11)
    _usuario(sql, 21, telefono="111")
    _vinculo(sql, 11, 21)

    datos = contacto.datos_de_contacto([10, 11], 1)
    assert datos[10]["tutores"] == []
    assert len(datos[11]["tutores"]) == 1


def test_sin_pacientes_no_consulta(monkeypatch):
    def no_deberia(*_):
        raise AssertionError("consultó la base sin pacientes")
    monkeypatch.setattr(dependencies, "ClinicSessionLocal", no_deberia)
    assert contacto.datos_de_contacto([], 1) == {}
    assert contacto.datos_de_contacto([None], 1) == {}


# ---------------------------------------------------------------------------
# Las rutas
# ---------------------------------------------------------------------------

def _con_contactos(monkeypatch, router, mapa):
    import routers.appointments as ra
    import routers.waitlist as rw
    modulo = ra if router == "turnos" else rw
    monkeypatch.setattr(modulo, "_datos_de_contacto", lambda patient_user_ids, clinic_id: dict(mapa))


def test_el_turno_de_un_paciente_a_cargo_dice_que_va_al_tutor(client, monkeypatch):
    apt_id = _insert(patient_user_id=5, patient_phone="351 999-0000")
    _con_contactos(monkeypatch, "turnos", {5: {"menor": None, "tutores": [ANA]}})

    fila = next(a for a in client.get(f"{BASE}/").json()["appointments"] if a["appointment_id"] == apt_id)
    assert fila["contacto"] == {"es_tutor": True, "nombre": "Ana Gómez", "telefono": "351 555-1111"}
    # El teléfono del paciente no cambia: Llamar sigue yendo a su ficha.
    assert fila["patient_phone"] == "351 999-0000"


def test_el_turno_de_un_paciente_sin_tutor_va_a_el(client, monkeypatch):
    apt_id = _insert(patient_user_id=5, patient_phone="351 999-0000")

    res = client.get(f"{BASE}/{apt_id}")
    assert res.status_code == 200
    assert res.json()["contacto"]["es_tutor"] is False
    assert res.json()["contacto"]["telefono"] == "351 999-0000"


def test_un_turno_solo_pide_su_contacto(client, monkeypatch):
    import routers.appointments as ra
    apt_id = _insert(patient_user_id=5)
    pedidos = []
    monkeypatch.setattr(ra, "_datos_de_contacto", lambda ids, clinica: pedidos.append(set(ids)) or {})

    client.get(f"{BASE}/{apt_id}")
    assert pedidos == [{5}]


def test_la_lista_de_turnos_pide_los_contactos_una_sola_vez(client, monkeypatch):
    import routers.appointments as ra
    _insert(patient_user_id=5)
    _insert(patient_user_id=6, days_ahead=2)
    pedidos = []
    monkeypatch.setattr(ra, "_datos_de_contacto", lambda ids, clinica: pedidos.append(set(ids)) or {})

    client.get(f"{BASE}/")
    assert len(pedidos) == 1
    assert {5, 6} <= pedidos[0]


def test_la_lista_de_espera_dice_a_quien_escribirle(receptionist_client, monkeypatch):
    _id_anotado(receptionist_client, 6)
    _con_contactos(monkeypatch, "espera", {6: {"menor": True, "tutores": [ANA]}})

    entrada = receptionist_client.get(f"{W}/").json()["entries"][0]
    assert entrada["contacto"] == {"es_tutor": True, "nombre": "Ana Gómez", "telefono": "351 555-1111"}


# ---------------------------------------------------------------------------
# Lo que encontró la revisión independiente
# ---------------------------------------------------------------------------

def test_un_paciente_que_se_paso_a_otra_clinica_no_trae_los_tutores_de_alla(base_de_la_clinica):
    """Todo de la clínica de quien pide, como en ortodoncia: ni el paciente ni sus tutores de otra."""
    sql = base_de_la_clinica
    _usuario(sql, 10, clinic_id=2, nacimiento="2020-01-01")
    _usuario(sql, 21, clinic_id=2, telefono="111")
    _vinculo(sql, 10, 21, clinic_id=2)

    assert contacto.datos_de_contacto([10], 1) == {}
    assert contacto.datos_de_contacto([10], 2)[10]["tutores"] == [{"nombre": "N A", "telefono": "111"}]


def test_si_la_consulta_falla_la_agenda_sigue_y_queda_un_error_sin_datos(monkeypatch, caplog):
    """Una columna renombrada en dental-clinic no tumba la agenda: cada paciente es su contacto."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(dependencies, "ClinicSessionLocal", sessionmaker(bind=engine))  # Sin tablas

    with caplog.at_level("ERROR", logger="contacto"):
        assert contacto.datos_de_contacto([10], 1) == {}
    [registro] = [r for r in caplog.records if r.name == "contacto"]
    assert registro.levelname == "ERROR"
    assert "OperationalError" in registro.getMessage()
    assert "users" not in registro.getMessage()  # El mensaje de la base no se copia


@pytest.mark.filterwarnings("error::UserWarning")
def test_la_respuesta_no_avisa_nada_al_serializar(client, monkeypatch):
    """Un `dict` en `contacto` hacía que Pydantic avisara con el teléfono adentro, y eso va al log."""
    apt_id = _insert(patient_user_id=5, patient_phone="351 999-0000")
    _con_contactos(monkeypatch, "turnos", {5: {"menor": None, "tutores": [ANA]}})

    assert client.get(f"{BASE}/{apt_id}").status_code == 200
    assert client.get(f"{BASE}/").status_code == 200


def test_el_contacto_usa_el_telefono_vigente_no_el_guardado(client, monkeypatch):
    """Sin tutor, el WhatsApp va al teléfono de hoy del paciente (#313), no al del día del turno."""
    import routers.appointments as ra
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja")
    monkeypatch.setattr(ra, "_telefonos_vigentes", lambda ids: {5: "351-1111-nueva"})

    assert client.get(f"{BASE}/{apt_id}").json()["contacto"]["telefono"] == "351-1111-nueva"


def test_la_lista_de_espera_usa_el_telefono_vigente(receptionist_client, monkeypatch):
    import routers.waitlist as rw
    _id_anotado(receptionist_client, 6)
    monkeypatch.setattr(rw, "_telefonos_vigentes", lambda ids: {6: "351-1111-nueva"})

    assert receptionist_client.get(f"{W}/").json()["entries"][0]["contacto"]["telefono"] == "351-1111-nueva"


def _hueco_con_candidatos(cliente, *pacientes):
    from test_agenda_recepcion import _patch
    from test_lista_espera import _turno, _utc
    for paciente in pacientes:
        _id_anotado(cliente, paciente, dentist=1)
    apt = _turno(_utc(10), dentist=1, paciente=5)
    assert _patch(cliente, apt, "CANCELLED").status_code == 200


def test_los_huecos_dicen_a_quien_escribirle_a_cada_candidato(receptionist_client, monkeypatch):
    _hueco_con_candidatos(receptionist_client, 6)
    _con_contactos(monkeypatch, "espera", {6: {"menor": None, "tutores": [ANA]}})

    [hueco] = receptionist_client.get(f"{W}/slots").json()["slots"]
    [candidato] = hueco["candidates"]
    assert candidato["contacto"] == {"es_tutor": True, "nombre": "Ana Gómez", "telefono": "351 555-1111"}


def test_los_huecos_piden_los_contactos_una_sola_vez(receptionist_client, monkeypatch):
    import routers.waitlist as rw
    _hueco_con_candidatos(receptionist_client, 6, 7)
    pedidos = []
    monkeypatch.setattr(rw, "_datos_de_contacto", lambda ids, clinica: pedidos.append((set(ids), clinica)) or {})

    receptionist_client.get(f"{W}/slots")
    assert pedidos == [({6, 7}, 1)]


def test_las_rutas_piden_los_contactos_con_la_clinica_del_turno(client, monkeypatch):
    import routers.appointments as ra
    apt_id = _insert(patient_user_id=5)
    pedidos = []
    monkeypatch.setattr(ra, "_datos_de_contacto", lambda ids, clinica: pedidos.append(clinica) or {})

    client.get(f"{BASE}/{apt_id}")
    client.get(f"{BASE}/")
    assert pedidos == [1, 1]
