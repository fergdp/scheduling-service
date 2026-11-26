"""
«A quién escribirle hoy» (#556) de punta a punta: las consultas contra tablas de verdad (en SQLite,
con las columnas que se leen y algunas que NO se leen, para mostrar que no viajan) y la API con sus
permisos. Las reglas, caso por caso, están en `test_contactos_reglas.py`.

La base de presupuestos se adjunta con `ATTACH ... AS dental_quoting_db`: la consulta usa el nombre
completo, como en MySQL.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import contactos_hoy
import dependencies
from conftest import TestingSessionLocal, engine as engine_de_turnos
from models import Appointment, AppointmentStatus, ContactMark
import schemas_contactos

URL = "/clinic-scheduling-api/v1/contactos"
HOY = date(2027, 3, 15)
AHORA_UTC = datetime(2027, 3, 15, 15, 0)
SECRETO = "SECRETO-CLINICO-556"
ROLES = {"patient": 1, "dentist": 2}


def hace(dias):
    return HOY - timedelta(days=dias)


class Clinica:
    """Las tablas de dental-clinic, clinical-history y quoting que lee el motor."""

    def __init__(self, engine):
        self.engine = engine

    def sql(self, sentencia, **datos):
        with self.engine.begin() as cx:
            cx.execute(text(sentencia), datos)

    def usuario(self, user_id, nombre, telefono=None, clinic_id=1, baja=False, rol="patient"):
        nombre, _, apellido = nombre.partition(" ")
        self.sql("INSERT INTO users VALUES (:id, :c, :n, :a, :t, NULL, :b)", id=user_id, c=clinic_id, n=nombre,
                 a=apellido, t=telefono, b="2027-01-01 10:00:00" if baja else None)
        self.sql("INSERT INTO users_roles VALUES (:id, :r)", id=user_id, r=ROLES[rol])

    def a_cargo(self, hijo, tutor, clinic_id=1):
        self.sql("INSERT INTO patient_guardians VALUES (:h, :t, 'madre', :c)", h=hijo, t=tutor, c=clinic_id)

    def prestacion(self, paciente, dias, profesional=4, clinic_id=1):
        self.sql("INSERT OR IGNORE INTO clinical_histories (history_id, patient_user_id, clinic_id, "
                 "affiliate_number) VALUES (:p, :p, :c, :s)", p=paciente, c=clinic_id, s=SECRETO)
        self.sql("INSERT INTO treatment_procedures (history_id, procedure_date, treatment_description, debit, "
                 "professional_id) VALUES (:h, :d, :s, 12345, :pr)",
                 h=paciente, d=hace(dias).isoformat(), s=SECRETO, pr=profesional)

    def ortodoncia(self, paciente, estado="ACTIVO", clinic_id=1):
        self.sql("INSERT INTO ortho_treatments (clinic_id, patient_user_id, estado, notas) VALUES (:c, :p, :e, :s)",
                 c=clinic_id, p=paciente, e=estado, s=SECRETO)

    def presupuesto(self, quote_id, paciente, dias, estado="SENT", vence=None, version_de=None, clinic_id=1,
                    dentista=4):
        self.sql("INSERT INTO dental_quoting_db.quote (id, clinic_id, patient_user_id, dentista_user_id, estado, "
                 "fecha_emision, fecha_vencimiento, version_of_quote_id, notas, total_paciente) "
                 "VALUES (:id, :c, :p, :d, :e, :f, :v, :vo, :s, 98765)",
                 id=quote_id, c=clinic_id, p=paciente, d=dentista, e=estado, f=hace(dias).isoformat(),
                 v=vence.isoformat() if vence else None, vo=version_de, s=SECRETO)


@pytest.fixture
def clinica(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _adjuntar(conexion, _):
        conexion.execute("ATTACH DATABASE ':memory:' AS dental_quoting_db")

    with engine.begin() as cx:
        for sentencia in [
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, clinic_id INTEGER, first_name TEXT, last_name TEXT, "
            "phone TEXT, date_of_birth DATE, deactivated_at DATETIME)",
            "CREATE TABLE patient_guardians (dependent_user_id INTEGER, guardian_user_id INTEGER, "
            "relationship TEXT, clinic_id INTEGER)",
            "CREATE TABLE roles (role_id INTEGER PRIMARY KEY, role_name TEXT)",
            "CREATE TABLE users_roles (user_id INTEGER, role_id INTEGER)",
            "CREATE TABLE clinics (clinic_id INTEGER PRIMARY KEY, clinic_name TEXT, owner_user_id INTEGER)",
            "CREATE TABLE clinical_histories (history_id INTEGER PRIMARY KEY, patient_user_id INTEGER, "
            "clinic_id INTEGER, affiliate_number TEXT)",
            "CREATE TABLE treatment_procedures (procedure_id INTEGER PRIMARY KEY AUTOINCREMENT, history_id INTEGER, "
            "procedure_date DATE, treatment_description TEXT, debit NUMERIC, professional_id INTEGER)",
            "CREATE TABLE ortho_treatments (id INTEGER PRIMARY KEY AUTOINCREMENT, clinic_id INTEGER, "
            "patient_user_id INTEGER, estado TEXT, notas TEXT)",
            "CREATE TABLE dental_quoting_db.quote (id INTEGER PRIMARY KEY, clinic_id INTEGER, patient_user_id INTEGER, "
            "dentista_user_id INTEGER, estado TEXT, fecha_emision DATE, fecha_vencimiento DATE, "
            "version_of_quote_id INTEGER, notas TEXT, total_paciente NUMERIC)",
        ]:
            cx.execute(text(sentencia))
        for nombre, role_id in ROLES.items():
            cx.execute(text("INSERT INTO roles VALUES (:id, :n)"), {"id": role_id, "n": nombre})
        cx.execute(text("INSERT INTO clinics VALUES (1, 'Consultorio Sur', 4), (2, 'Otra Clínica', 99)"))
    monkeypatch.setattr(dependencies, "ClinicSessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(contactos_hoy, "hoy_en_cordoba", lambda: HOY)
    monkeypatch.setattr(contactos_hoy, "_ahora_utc", lambda: AHORA_UTC)
    c = Clinica(engine)
    c.usuario(4, "Analía Ríos", "351 444-0004", rol="dentist")
    return c


def turno(paciente, inicio_utc, estado=AppointmentStatus.SCHEDULED, odontologo=4, clinic_id=1, borrado=False):
    db = TestingSessionLocal()
    db.add(Appointment(clinic_id=clinic_id, patient_user_id=paciente, dentist_user_id=odontologo,
                       patient_name="x", start_time_utc=inicio_utc, end_time_utc=inicio_utc + timedelta(minutes=30),
                       status=estado, deleted_at=AHORA_UTC if borrado else None))
    db.commit()
    db.close()


def hoy(cliente, **params):
    r = cliente.get(f"{URL}/hoy", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def quienes(respuesta):
    return [(a["motivo"], [p["patient_user_id"] for p in a["pacientes"]]) for a in respuesta["avisos"]]


def marcar(cliente, motivo, accion, *pacientes, hasta=None, esperado=201):
    cuerpo = {"motivo": motivo, "accion": accion, "pacientes": [
        p if isinstance(p, dict) else {"patient_user_id": p} for p in pacientes]}
    if hasta:
        cuerpo["hasta"] = hasta
    r = cliente.post(f"{URL}/marcas", json=cuerpo)
    assert r.status_code == esperado, r.text
    return r.json()


# ---------------------------------------------------------------------------
# La lista del día
# ---------------------------------------------------------------------------

def test_la_lista_del_dia_de_punta_a_punta(clinica, receptionist_client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 400)
    clinica.prestacion(10, 200)            # la última atención: hace 200 días
    clinica.usuario(11, "Carlos Díaz", "351 555-0011")
    clinica.presupuesto(30, 11, 12)
    clinica.usuario(12, "Reciente Uno", "351 555-0012")
    clinica.prestacion(12, 100)            # todavía no le toca

    r = hoy(receptionist_client)
    assert r["hoy"] == "2027-03-15" and r["tope"] == 10
    assert quienes(r) == [("PRESUPUESTO", [11]), ("CONTROL", [10])]
    presupuesto, control = r["avisos"]
    assert presupuesto["pacientes"] == [{"patient_user_id": 11, "nombre": "Carlos Díaz", "quote_id": 30}]
    assert presupuesto["fecha_presupuesto"] == hace(12).isoformat() and not presupuesto["presupuesto_vencido"]
    assert presupuesto["profesional"] == "Analía Ríos"
    assert control["numero_whatsapp"] == "5493515550010"
    assert control["contacto"] == {"es_tutor": False, "nombre": "Juan Pérez", "telefono": "351 555-0010"}
    assert control["razon"] == "No viene hace 6 meses" and control["dias"] == 200
    assert control["mensaje"].startswith("Hola Juan, ¿cómo estás? Soy Analía Ríos, de Consultorio Sur. Ya pasaron 6")
    assert control["clave"] == "CONTROL:5493515550010" and control["es_chicos"] is False
    assert r["sin_telefono"] == [] and r["motivos_con_error"] == []


def test_los_chicos_van_al_tutor_en_un_solo_aviso(clinica, client):
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    for hijo, nombre in [(21, "TOMÁS GÓMEZ"), (22, "Sofía Gómez")]:
        clinica.usuario(hijo, nombre, "351 999-9999")
        clinica.a_cargo(hijo, 20)
        clinica.prestacion(hijo, 250)
    r = hoy(client)
    assert quienes(r) == [("CONTROL", [21, 22])]
    aviso = r["avisos"][0]
    assert aviso["es_chicos"] is True and aviso["numero_whatsapp"] == "5493515550020"
    assert "Hola Laura" in aviso["mensaje"] and "de Tomás y Sofía" in aviso["mensaje"]


def test_presupuestos_que_entran_y_que_no(clinica, client):
    for i in range(40, 47):
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}")
    clinica.presupuesto(1, 40, 7)                                   # entra
    clinica.presupuesto(2, 41, 6)                                   # muy nuevo
    clinica.presupuesto(3, 42, 91)                                  # muy viejo
    clinica.presupuesto(4, 43, 30, estado="ACCEPTED")
    clinica.presupuesto(5, 44, 30)                                  # tiene una versión más nueva
    clinica.presupuesto(6, 44, 20, estado="DRAFT", version_de=5)
    clinica.presupuesto(7, 45, 90, vence=hace(30))                  # vencido: entra igual
    clinica.presupuesto(8, 46, 30, clinic_id=2)                     # de otra clínica
    r = hoy(client)
    assert quienes(r) == [("PRESUPUESTO", [45]), ("PRESUPUESTO", [40])]
    assert r["avisos"][0]["presupuesto_vencido"] is True
    assert "te lo podemos actualizar" in r["avisos"][0]["mensaje"]


def test_la_version_nueva_es_la_que_cuenta(clinica, client):
    clinica.usuario(40, "Ana X", "351 555-0040")
    clinica.presupuesto(1, 40, 40)
    clinica.presupuesto(2, 40, 20, version_de=1)
    assert [a["pacientes"][0]["quote_id"] for a in hoy(client)["avisos"]] == [2]


def test_lo_que_saca_de_control(clinica, client):
    for i, (dias, extra) in enumerate([
        (200, "orto"), (200, "orto cerrada"), (200, "turno"), (200, "turno pasado"), (200, "baja"),
        (200, "otra clinica"), (200, "turno borrado"),
    ], start=50):
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}", baja=extra == "baja",
                        clinic_id=2 if extra == "otra clinica" else 1)
        clinica.prestacion(i, dias, clinic_id=2 if extra == "otra clinica" else 1)
    clinica.ortodoncia(50)
    clinica.ortodoncia(51, estado="FINALIZADO")
    turno(52, AHORA_UTC + timedelta(days=3))
    turno(53, AHORA_UTC - timedelta(days=3), estado=AppointmentStatus.CANCELLED)  # uno cancelado no cuenta
    turno(56, AHORA_UTC + timedelta(days=3), borrado=True)
    assert quienes(hoy(client)) == [("CONTROL", [51]), ("CONTROL", [53]), ("CONTROL", [56])]


def test_las_prestaciones_son_solo_de_la_clinica(clinica):
    """
    Directo contra la consulta: desde la API no se nota, porque el paciente de otra clínica después
    no pasa el filtro de pacientes (lo encontró un sabotaje).
    """
    clinica.prestacion(10, 200, clinic_id=1)
    clinica.prestacion(11, 300, profesional=9, clinic_id=2)
    assert contactos_hoy.leer_ultimas_prestaciones(1, HOY) == {10: (hace(200), 4)}
    assert contactos_hoy.leer_ultimas_prestaciones(2, HOY) == {11: (hace(300), 9)}


def test_de_dos_prestaciones_del_mismo_dia_vale_la_ultima_cargada(clinica):
    clinica.prestacion(10, 200, profesional=4)
    clinica.prestacion(10, 200, profesional=9)
    clinica.prestacion(10, 300, profesional=7)
    assert contactos_hoy.leer_ultimas_prestaciones(1, HOY) == {10: (hace(200), 9)}


def test_un_turno_atendido_cuenta_como_atencion(clinica, client):
    clinica.usuario(60, "Sin Prestación", "351 555-0060")
    clinica.usuario(9, "Otro Odontólogo", rol="dentist")
    turno(60, AHORA_UTC - timedelta(days=200), estado=AppointmentStatus.COMPLETED, odontologo=9)
    clinica.usuario(61, "Atendido Hace Poco", "351 555-0061")
    clinica.prestacion(61, 300)
    turno(61, AHORA_UTC - timedelta(days=20), estado=AppointmentStatus.COMPLETED)
    r = hoy(client)
    assert quienes(r) == [("CONTROL", [60])]
    assert r["avisos"][0]["profesional"] == "Otro Odontólogo"


def test_filtro_por_odontologo(clinica, client):
    clinica.usuario(9, "Otro Odontólogo", rol="dentist")
    for i, prof in [(70, 4), (71, 9)]:
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}")
        clinica.prestacion(i, 200, profesional=prof)
    clinica.usuario(72, "P72 X", "351 555-0072")
    clinica.presupuesto(1, 72, 10, dentista=9)
    assert quienes(hoy(client, dentist_id=9)) == [("PRESUPUESTO", [72]), ("CONTROL", [71])]
    assert quienes(hoy(client, dentist_id=4)) == [("CONTROL", [70])]


def test_sin_telefono_va_aparte(clinica, client):
    clinica.usuario(80, "Mal Cargado", "4555-1234")
    clinica.prestacion(80, 200)
    r = hoy(client)
    assert r["avisos"] == []
    assert r["sin_telefono"] == [{"patient_user_id": 80, "nombre": "Mal Cargado", "motivo": "CONTROL"}]


def test_tope_de_diez(clinica, client):
    for i in range(100, 115):
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}")
        clinica.prestacion(i, 200 + i)
    assert len(hoy(client)["avisos"]) == 10


# ---------------------------------------------------------------------------
# Si algo no se puede leer
# ---------------------------------------------------------------------------

def test_sin_presupuestos_los_controles_salen_igual(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.sql("DROP TABLE dental_quoting_db.quote")
    r = hoy(client)
    assert r["motivos_con_error"] == ["PRESUPUESTO"] and quienes(r) == [("CONTROL", [10])]


@pytest.mark.parametrize("tabla", ["treatment_procedures", "ortho_treatments"])
def test_sin_prestaciones_u_ortodoncia_no_sale_ningun_control(clinica, client, tabla):
    """Sin ortodoncia no se sabe a quién sacar: mejor ningún control que llamar a uno en tratamiento."""
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.usuario(11, "Carlos Díaz", "351 555-0011")
    clinica.presupuesto(30, 11, 12)
    clinica.sql(f"DROP TABLE {tabla}")
    r = hoy(client)
    assert r["motivos_con_error"] == ["CONTROL"] and quienes(r) == [("PRESUPUESTO", [11])]


def test_sin_pacientes_no_sale_nada(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.sql("ALTER TABLE users RENAME TO users_viejos")
    r = hoy(client)
    assert r["avisos"] == [] and r["motivos_con_error"] == ["CONTROL", "PRESUPUESTO"]


def test_sin_el_nombre_de_la_clinica_el_mensaje_sale_igual(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.sql("DROP TABLE clinics")
    assert hoy(client)["avisos"][0]["mensaje"].startswith("Hola Juan, ¿cómo estás? Soy Analía Ríos. Ya")


# ---------------------------------------------------------------------------
# Nada clínico en la respuesta
# ---------------------------------------------------------------------------

# Los campos de cada clase de la respuesta. ⚠️ Un campo nuevo se suma acá a propósito, después de
# pensar si la recepción lo puede ver (como `OrthoRecallSinDatosClinicosGuardTest`, #354).
CAMPOS_PERMITIDOS = {
    schemas_contactos.AvisosDelDia: {"hoy", "tope", "avisos", "sin_telefono", "motivos_con_error"},
    schemas_contactos.Aviso: {
        "clave", "motivo", "es_chicos", "pacientes", "contacto", "numero_whatsapp", "razon", "dias",
        "fecha_presupuesto", "presupuesto_vencido", "profesional_user_id", "profesional", "mensaje",
    },
    schemas_contactos.PacienteDelAviso: {"patient_user_id", "nombre", "quote_id"},
    schemas_contactos.SinTelefono: {"patient_user_id", "nombre", "motivo"},
    schemas_contactos.ContactoParaEscribir: {"es_tutor", "nombre", "telefono"},
    schemas_contactos.MarcaGuardada: {"mark_id", "patient_user_id", "motivo", "accion", "quote_id", "fecha", "hasta"},
    schemas_contactos.MarcasGuardadas: {"marcas"},
    schemas_contactos.ResenaDelPaciente: {"veces", "ultima", "numero_whatsapp", "contacto", "mensaje"},
    schemas_contactos.Resenas: {"link_configurado", "pacientes"},
    schemas_contactos.ConfigDeContactos: {
        "link_resena", "textos", "textos_de_fabrica", "marcadores", "dias_control", "tope_diario",
        "motivos_activos", "puede_editar",
    },
    schemas_contactos.TextosDeContactos: {"control", "chicos", "presupuesto", "resena"},
    schemas_contactos.VistaPrevia: {"mensaje"},
}


@pytest.mark.parametrize("clase", list(CAMPOS_PERMITIDOS), ids=lambda c: c.__name__)
def test_los_campos_de_la_respuesta_estan_contados(clase):
    assert set(clase.model_fields) == CAMPOS_PERMITIDOS[clase]


def test_la_recepcion_no_recibe_nada_clinico(clinica, receptionist_client):
    """Lo clínico está cargado con una marca; la marca no aparece en nada de lo que responde."""
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.ortodoncia(10, estado="FINALIZADO")
    clinica.usuario(11, "Carlos Díaz", "351 555-0011")
    clinica.presupuesto(30, 11, 12)
    texto = receptionist_client.get(f"{URL}/hoy").text
    assert '"avisos":[{' in texto and "Juan" in texto and "Carlos" in texto
    for prohibido in [SECRETO, "12345", "98765"]:
        assert prohibido not in texto


def test_las_consultas_no_piden_columnas_clinicas(clinica, client, monkeypatch):
    """El GRANT por columna (#384) lo garantiza en prod; acá, que ninguna consulta las nombre."""
    pedidas = []
    original = contactos_hoy._consulta
    monkeypatch.setattr(contactos_hoy, "_consulta", lambda sql, p, q: pedidas.append(sql) or original(sql, p, q))
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.presupuesto(30, 10, 12)
    hoy(client)
    todo = " ".join(pedidas).lower()
    assert len(pedidas) >= 5
    for columna in ["treatment_description", "debit", "credit", "notas", "total", "affiliate_number", "*"]:
        assert columna not in todo


# ---------------------------------------------------------------------------
# Marcas
# ---------------------------------------------------------------------------

def test_le_escribi_lo_saca_y_deshacer_lo_devuelve(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    guardada = marcar(client, "CONTROL", "ESCRITO", 10)["marcas"]
    assert guardada == [{"mark_id": guardada[0]["mark_id"], "patient_user_id": 10, "motivo": "CONTROL",
                         "accion": "ESCRITO", "quote_id": None, "fecha": "2027-03-15", "hasta": None}]
    assert hoy(client)["avisos"] == []
    assert client.delete(f"{URL}/marcas/{guardada[0]['mark_id']}").status_code == 204
    assert quienes(hoy(client)) == [("CONTROL", [10])]
    # Deshacer dos veces es lo mismo que una.
    assert client.delete(f"{URL}/marcas/{guardada[0]['mark_id']}").status_code == 204
    db = TestingSessionLocal()
    assert db.query(ContactMark).filter(ContactMark.accion == "DESHECHO").count() == 1
    db.close()


def test_la_marca_guarda_quien_y_la_clinica(clinica, receptionist_client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    marcar(receptionist_client, "CONTROL", "ESCRITO", 10)
    db = TestingSessionLocal()
    fila = db.query(ContactMark).one()
    assert (fila.clinic_id, fila.marked_by_user_id) == (1, 50)
    db.close()


def _aviso(cliente, motivo="CONTROL"):
    a = next(x for x in hoy(cliente)["avisos"] if x["motivo"] == motivo)
    return [{"patient_user_id": p["patient_user_id"], **({"quote_id": p["quote_id"]} if p["quote_id"] else {})}
            for p in a["pacientes"]]


def test_el_tope_es_del_dia_no_de_cada_pedido(clinica, client):
    """Lo encontró la revisión: marcar los 10 y recargar traía otros 10."""
    for i in range(100, 115):
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}")
        clinica.prestacion(i, 200 + i)
    for _ in range(4):
        marcar(client, "CONTROL", "ESCRITO", *_aviso(client))
    assert len(hoy(client)["avisos"]) == 6
    for a in hoy(client)["avisos"]:
        marcar(client, "CONTROL", "ESCRITO", *[p["patient_user_id"] for p in a["pacientes"]])
    assert hoy(client)["avisos"] == []


def test_posponer_o_descartar_no_libera_un_lugar_del_tope(clinica, client):
    """Sólo cuentan los mensajes mandados: posponer no es escribir."""
    for i in range(100, 112):
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}")
        clinica.prestacion(i, 200 + i)
    marcar(client, "CONTROL", "POSPUESTO", *_aviso(client))
    assert len(hoy(client)["avisos"]) == 10


def test_un_numero_que_ya_recibio_un_mensaje_hoy_no_recibe_otro(clinica, client):
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    for hijo in (21, 22):
        clinica.usuario(hijo, f"Hijo{hijo} Gómez", None)
        clinica.a_cargo(hijo, 20)
    clinica.presupuesto(30, 21, 12)
    clinica.prestacion(22, 250)
    assert quienes(hoy(client)) == [("PRESUPUESTO", [21])]
    marcar(client, "PRESUPUESTO", "ESCRITO", *_aviso(client, "PRESUPUESTO"))
    assert hoy(client)["avisos"] == []


def test_un_pedido_de_resena_de_hoy_tambien_ocupa_el_numero(clinica, client):
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    for hijo in (21, 22):
        clinica.usuario(hijo, f"Hijo{hijo} Gómez", None)
        clinica.a_cargo(hijo, 20)
    clinica.prestacion(22, 250)
    marcar(client, "RESENA", "ESCRITO", 21)
    assert hoy(client)["avisos"] == []


def test_dos_adultos_con_el_mismo_numero_el_segundo_espera_de_verdad(clinica, client):
    for i in (10, 11):
        clinica.usuario(i, f"P{i} X", "351 555-0010")
        clinica.prestacion(i, 200)
    marcar(client, "CONTROL", "ESCRITO", *_aviso(client))
    assert hoy(client)["avisos"] == []


def test_un_paciente_tocado_hoy_no_vuelve_hoy_por_otro_motivo(clinica, client, monkeypatch):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.presupuesto(30, 10, 12)
    marcar(client, "PRESUPUESTO", "POSPUESTO", *_aviso(client, "PRESUPUESTO"))
    assert hoy(client)["avisos"] == []
    monkeypatch.setattr(contactos_hoy, "hoy_en_cordoba", lambda: HOY + timedelta(days=1))
    assert quienes(hoy(client)) == [("CONTROL", [10])]


def test_el_control_vuelve_a_los_60_dias(clinica, client, monkeypatch):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    marcar(client, "CONTROL", "ESCRITO", 10)
    monkeypatch.setattr(contactos_hoy, "hoy_en_cordoba", lambda: HOY + timedelta(days=59))
    assert hoy(client)["avisos"] == []
    monkeypatch.setattr(contactos_hoy, "hoy_en_cordoba", lambda: HOY + timedelta(days=60))
    assert quienes(hoy(client)) == [("CONTROL", [10])]


def test_posponer_y_no_sugerir_nada(clinica, client, monkeypatch):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.usuario(11, "Carlos Díaz", "351 555-0011")
    clinica.prestacion(11, 200)
    pospuesta = marcar(client, "CONTROL", "POSPUESTO", 10)["marcas"][0]
    assert pospuesta["hasta"] == "2027-03-22"
    marcar(client, "CONTROL", "NO_SUGERIR_NADA", 11)
    assert hoy(client)["avisos"] == []
    monkeypatch.setattr(contactos_hoy, "hoy_en_cordoba", lambda: date(2027, 3, 23))
    assert quienes(hoy(client)) == [("CONTROL", [10])]


def test_un_presupuesto_se_marca_con_el_suyo(clinica, client):
    clinica.usuario(11, "Carlos Díaz", "351 555-0011")
    clinica.usuario(12, "Otra Persona", "351 555-0012")
    clinica.presupuesto(30, 11, 12)
    clinica.presupuesto(31, 12, 12)
    clinica.presupuesto(32, 11, 12, clinic_id=2)
    for quote_id in [31, 32, 999]:
        marcar(client, "PRESUPUESTO", "ESCRITO", {"patient_user_id": 11, "quote_id": quote_id}, esperado=404)
    marcar(client, "PRESUPUESTO", "NO_SUGERIR", {"patient_user_id": 11, "quote_id": 30})
    assert quienes(hoy(client)) == [("PRESUPUESTO", [12])]


@pytest.mark.parametrize("cuerpo", [
    {"motivo": "RESENA", "accion": "POSPUESTO", "pacientes": [{"patient_user_id": 10}]},
    {"motivo": "CONTROL", "accion": "ESCRITO", "pacientes": [{"patient_user_id": 10}], "hasta": "2027-03-20"},
    {"motivo": "PRESUPUESTO", "accion": "ESCRITO", "pacientes": [{"patient_user_id": 10}]},
    {"motivo": "CONTROL", "accion": "ESCRITO", "pacientes": [{"patient_user_id": 10, "quote_id": 3}]},
    {"motivo": "CONTROL", "accion": "ESCRITO", "pacientes": [{"patient_user_id": 10}, {"patient_user_id": 10}]},
    {"motivo": "CONTROL", "accion": "ESCRITO", "pacientes": []},
    {"motivo": "CONTROL", "accion": "DESHECHO", "pacientes": [{"patient_user_id": 10}]},
    {"motivo": "OTRO", "accion": "ESCRITO", "pacientes": [{"patient_user_id": 10}]},
    {"motivo": "CONTROL", "accion": "POSPUESTO", "pacientes": [{"patient_user_id": 10}], "hasta": "2027-03-15"},
    {"motivo": "CONTROL", "accion": "POSPUESTO", "pacientes": [{"patient_user_id": 10}], "hasta": "2027-06-14"},
], ids=["resena-pospuesta", "hasta-sin-posponer", "presupuesto-sin-quote", "quote-en-control", "repetido",
        "sin-pacientes", "deshecho-a-mano", "motivo-raro", "posponer-hoy", "posponer-mas-de-90"])
def test_pedidos_invalidos(clinica, client, cuerpo):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    assert client.post(f"{URL}/marcas", json=cuerpo).status_code == 422


def test_posponer_hasta_90_dias(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    marcar(client, "CONTROL", "POSPUESTO", 10, hasta="2027-06-13")


def marca_en_la_base(clinic_id, paciente, motivo="CONTROL", accion="ESCRITO"):
    """Una marca cargada directo, para probar desde otra clínica (dos clientes en un test comparten los overrides)."""
    db = TestingSessionLocal()
    fila = ContactMark(clinic_id=clinic_id, patient_user_id=paciente, motivo=motivo, accion=accion,
                       fecha=HOY, marked_by_user_id=4)
    db.add(fila)
    db.commit()
    mark_id = fila.mark_id
    db.close()
    return mark_id


def test_un_paciente_de_otra_clinica_no_se_marca(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.usuario(90, "De Otra", "351 555-0090", clinic_id=2)
    marcar(client, "CONTROL", "ESCRITO", 90, esperado=404)
    marcar(client, "CONTROL", "ESCRITO", 999, esperado=404)
    marcar(client, "CONTROL", "ESCRITO", 10, 90, esperado=404)
    db = TestingSessionLocal()
    assert db.query(ContactMark).count() == 0
    db.close()
    assert client.delete(f"{URL}/marcas/{marca_en_la_base(2, 90)}").status_code == 404
    assert client.delete(f"{URL}/marcas/999999").status_code == 404


def test_una_deshecho_no_se_deshace(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    mark_id = marcar(client, "CONTROL", "ESCRITO", 10)["marcas"][0]["mark_id"]
    client.delete(f"{URL}/marcas/{mark_id}")
    db = TestingSessionLocal()
    deshecho = db.query(ContactMark).filter(ContactMark.accion == "DESHECHO").one().mark_id
    db.close()
    assert client.delete(f"{URL}/marcas/{deshecho}").status_code == 404


def test_si_no_se_puede_verificar_el_paciente_no_se_marca(clinica, client):
    clinica.sql("DROP TABLE users")
    marcar(client, "CONTROL", "ESCRITO", 10, esperado=503)


# ---------------------------------------------------------------------------
# Reseñas
# ---------------------------------------------------------------------------

def test_cuantas_veces_se_pidio_la_resena(clinica, client, monkeypatch):
    for i in (10, 11):
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}")
    marcar(client, "RESENA", "ESCRITO", 10)
    monkeypatch.setattr(contactos_hoy, "hoy_en_cordoba", lambda: HOY + timedelta(days=5))
    marcar(client, "RESENA", "ESCRITO", 10)
    monkeypatch.setattr(contactos_hoy, "hoy_en_cordoba", lambda: HOY + timedelta(days=6))
    deshecha = marcar(client, "RESENA", "ESCRITO", 10)["marcas"][0]["mark_id"]
    client.delete(f"{URL}/marcas/{deshecha}")
    marcar(client, "CONTROL", "ESCRITO", 11)
    r = client.get(f"{URL}/resenas", params={"patient_ids": "10,11,12"})
    assert r.status_code == 200
    cero = {"veces": 0, "ultima": None, "numero_whatsapp": None, "contacto": None, "mensaje": None}
    assert r.json() == {"link_configurado": False, "pacientes": {
        "10": {**cero, "veces": 2, "ultima": "2027-03-20"}, "11": cero, "12": cero}}


def test_las_resenas_de_otra_clinica_no_se_ven(clinica, client):
    marca_en_la_base(2, 10, motivo="RESENA")
    r = client.get(f"{URL}/resenas", params={"patient_ids": "10"})
    assert r.json()["pacientes"] == {"10": {"veces": 0, "ultima": None, "numero_whatsapp": None,
                                            "contacto": None, "mensaje": None}}


def test_una_marca_de_otra_clinica_no_saca_a_nadie_de_la_lista(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    marca_en_la_base(2, 10, accion="NO_SUGERIR_NADA")
    assert quienes(hoy(client)) == [("CONTROL", [10])]


@pytest.mark.parametrize("ids", ["", "a,b", "0", ",".join(str(i) for i in range(1, 202))])
def test_resenas_con_ids_invalidos(clinica, client, ids):
    assert client.get(f"{URL}/resenas", params={"patient_ids": ids}).status_code == 422


# ---------------------------------------------------------------------------
# Quién entra
# ---------------------------------------------------------------------------

PEDIDOS_VALIDOS = [
    ("get", f"{URL}/hoy", None),
    ("post", f"{URL}/marcas", {"motivo": "CONTROL", "accion": "ESCRITO", "pacientes": [{"patient_user_id": 10}]}),
    ("delete", f"{URL}/marcas/1", None),
    ("get", f"{URL}/resenas?patient_ids=10", None),
]


@pytest.mark.parametrize("metodo, ruta, cuerpo", PEDIDOS_VALIDOS, ids=lambda x: str(x)[:30])
def test_un_paciente_no_entra(clinica, patient_client, metodo, ruta, cuerpo):
    """Con un pedido válido: uno inválido daría 422 antes de mirar el permiso y no probaría nada."""
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    r = getattr(patient_client, metodo)(ruta, **({"json": cuerpo} if cuerpo else {}))
    assert r.status_code == 403


def test_sin_sesion_401():
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as c:
        assert c.get(f"{URL}/hoy").status_code == 401


def test_marcar_sin_csrf_no_pasa(clinica, client_sin_reraise):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    r = client_sin_reraise.post(f"{URL}/marcas", json=PEDIDOS_VALIDOS[1][2])
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# La tabla
# ---------------------------------------------------------------------------

def test_la_migracion_crea_la_tabla_que_declara_el_modelo():
    """La migración y `models.ContactMark` tienen que decir lo mismo: los tests arman la tabla del modelo."""
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import pathlib

    ruta = pathlib.Path(__file__).parent.parent / "alembic" / "versions" / "b4d8e2f6a9c1_contact_marks.py"
    spec = importlib.util.spec_from_file_location("migracion_556", ruta)
    migracion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracion)

    otra = create_engine("sqlite://")
    with otra.begin() as cx:
        import alembic.op as op_module  # noqa: F401
        from alembic import op
        with Operations.context(MigrationContext.configure(cx)):
            migracion.upgrade()
        de_la_migracion = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(cx).get_columns("contact_marks")}
        indices = {i["name"] for i in inspect(cx).get_indexes("contact_marks")}
    with engine_de_turnos.connect() as cx:
        del_modelo = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(cx).get_columns("contact_marks")}
    assert de_la_migracion == del_modelo
    assert "ix_contact_marks_clinic_patient" in indices
    assert migracion.MOTIVOS == __import__("models").CONTACTO_MOTIVOS
    assert migracion.ACCIONES == __import__("models").CONTACTO_ACCIONES


# ---------------------------------------------------------------------------
# Lo que trajo la revisión independiente (2026-11-20)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("estado, entra", [
    ("ACTIVO", False), ("EN_PAUSA", False), ("CONTENCION", False),
    ("CONSULTA", True), ("PENDIENTE_INICIO", True), ("OBSERVACION", True), ("FINALIZADO", True),
])
def test_solo_saca_la_ortodoncia_en_tratamiento(clinica, client, estado, entra):
    """«Controles a agendar» (#354) llama sólo a los ACTIVO: una consulta de ortodoncia no deja sin control."""
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.ortodoncia(10, estado=estado)
    assert quienes(hoy(client)) == ([("CONTROL", [10])] if entra else [])


@pytest.mark.parametrize("estado, cuenta", [
    (AppointmentStatus.SCHEDULED, True), (AppointmentStatus.CONFIRMED, True), (AppointmentStatus.ARRIVED, True),
    (AppointmentStatus.COMPLETED, True), (AppointmentStatus.CANCELLED, False), (AppointmentStatus.NO_SHOW, False),
])
def test_un_turno_pasado_que_no_se_cancelo_cuenta_como_que_vino(clinica, client, estado, cuenta):
    """La agenda no se cierra: un SCHEDULED de la semana pasada casi siempre es alguien que vino."""
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    turno(10, AHORA_UTC - timedelta(days=7), estado=estado)
    assert quienes(hoy(client)) == ([] if cuenta else [("CONTROL", [10])])


def test_una_fecha_futura_no_esconde_al_paciente(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.prestacion(10, -300)                                  # un error de tipeo: dentro de 300 días
    turno(10, AHORA_UTC + timedelta(days=30), estado=AppointmentStatus.COMPLETED)
    r = hoy(client)
    assert quienes(r) == [("CONTROL", [10])] and r["avisos"][0]["dias"] == 200


def test_las_prestaciones_de_hace_mas_de_tres_anos_no_se_leen(clinica):
    clinica.prestacion(10, 1095)
    clinica.prestacion(11, 1096)
    assert contactos_hoy.leer_ultimas_prestaciones(1, HOY) == {10: (hace(1095), 4)}


def test_hermanos_con_presupuesto_uno_vencido(clinica, client):
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    for hijo in (21, 22):
        clinica.usuario(hijo, f"Hijo{hijo} Gómez", None)
        clinica.a_cargo(hijo, 20)
    clinica.presupuesto(30, 21, 30)
    clinica.presupuesto(31, 22, 20, vence=hace(1))
    aviso = hoy(client)["avisos"][0]
    assert [p["quote_id"] for p in aviso["pacientes"]] == [30, 31]
    assert aviso["presupuesto_vencido"] is True
    assert "con los presupuestos de Hijo21 y Hijo22. " in aviso["mensaje"]
    assert aviso["mensaje"].endswith("Como ya pasó la fecha de validez, te los podemos actualizar.")


def test_dos_toques_en_pedir_resena_cuentan_uno(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    primera = marcar(client, "RESENA", "ESCRITO", 10)["marcas"][0]
    segunda = marcar(client, "RESENA", "ESCRITO", 10)["marcas"][0]
    assert primera == segunda
    assert client.get(f"{URL}/resenas", params={"patient_ids": "10"}).json()["pacientes"]["10"]["veces"] == 1
    # Deshecha, el toque siguiente sí anota una nueva.
    client.delete(f"{URL}/marcas/{primera['mark_id']}")
    assert marcar(client, "RESENA", "ESCRITO", 10)["marcas"][0]["mark_id"] != primera["mark_id"]


def test_la_vista_del_odontologo_es_la_de_la_clinica_filtrada(clinica, client):
    """Un número que la clínica le da al presupuesto de A no recibe el control en la vista de B."""
    clinica.usuario(9, "Otro Odontólogo", rol="dentist")
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    for hijo in (21, 22):
        clinica.usuario(hijo, f"Hijo{hijo} Gómez", None)
        clinica.a_cargo(hijo, 20)
    clinica.presupuesto(30, 21, 12, dentista=4)
    clinica.prestacion(22, 250, profesional=9)
    assert quienes(hoy(client)) == [("PRESUPUESTO", [21])]
    assert quienes(hoy(client, dentist_id=9)) == []
    assert quienes(hoy(client, dentist_id=4)) == [("PRESUPUESTO", [21])]


def test_las_marcas_viejas_que_ya_no_sacan_a_nadie_no_se_leen(clinica, client):
    db = TestingSessionLocal()
    for dias, motivo, accion, quote, hasta in [
        (61, "CONTROL", "ESCRITO", None, None),            # ya no saca
        (59, "CONTROL", "ESCRITO", None, None),            # saca
        (400, "CONTROL", "NO_SUGERIR", None, None),        # para siempre
        (400, "PRESUPUESTO", "ESCRITO", 30, None),         # para siempre
        (100, "CONTROL", "POSPUESTO", None, HOY),          # sigue
        (100, "CONTROL", "POSPUESTO", None, hace(1)),      # venció
    ]:
        db.add(ContactMark(clinic_id=1, patient_user_id=10, motivo=motivo, accion=accion, quote_id=quote,
                           fecha=hace(dias), hasta=hasta, marked_by_user_id=4))
    db.commit()
    leidas = contactos_hoy.leer_marcas(db, 1, para_el_dia=HOY)
    db.close()
    assert sorted((m.accion, (HOY - m.fecha).days) for m in leidas) == [
        ("ESCRITO", 59), ("ESCRITO", 400), ("NO_SUGERIR", 400), ("POSPUESTO", 100)]


def test_un_turno_viejo_sin_cerrar_es_la_ultima_atencion_no_un_turno_por_venir(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    turno(10, AHORA_UTC - timedelta(days=250))
    r = hoy(client)
    assert quienes(r) == [("CONTROL", [10])] and r["avisos"][0]["dias"] == 250
