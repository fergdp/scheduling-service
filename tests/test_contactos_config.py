"""
La configuración de «A quién escribirle hoy» (#558): quién la edita, qué acepta, que el panel la use, y
el mensaje del botón «Pedir reseña». Reusa la base de `test_contactos_api.py`.
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, inspect

import contactos_config
import contactos_hoy
from conftest import TestingSessionLocal, engine as engine_de_turnos
from models import ContactMark
from test_contactos_api import HOY, URL, clinica, hace, hoy, marcar, quienes  # noqa: F401  (fixture)


def dueno(clinica_, user_id):
    clinica_.sql("UPDATE clinics SET owner_user_id = :u WHERE clinic_id = 1", u=user_id)


def config_valida(**extra):
    cuerpo = {"link_resena": "https://g.page/r/consultorio-sur/review", "textos": {},
              "dias_control": 180, "tope_diario": 10, "motivos_activos": ["PRESUPUESTO", "CONTROL"]}
    cuerpo.update(extra)
    return cuerpo


# ---------------------------------------------------------------------------
# Quién la ve y quién la cambia
# ---------------------------------------------------------------------------

def test_sin_fila_vale_lo_de_fabrica(clinica, receptionist_client):
    r = receptionist_client.get(f"{URL}/config")
    assert r.status_code == 200
    datos = r.json()
    assert datos["link_resena"] is None
    assert datos["textos"] == {"control": None, "chicos": None, "presupuesto": None, "resena": None}
    assert datos["textos_de_fabrica"] == contactos_config.DE_FABRICA
    assert (datos["dias_control"], datos["tope_diario"]) == (180, 10)
    assert datos["motivos_activos"] == ["PRESUPUESTO", "CONTROL"]
    assert datos["marcadores"] == list(contactos_config.MARCADORES)
    assert datos["puede_editar"] is False


def test_el_admin_edita(clinica, client):
    assert client.get(f"{URL}/config").json()["puede_editar"] is True
    assert client.put(f"{URL}/config", json=config_valida()).status_code == 200


def test_el_dueno_edita_y_otro_odontologo_no(clinica, other_dentist_client):
    dueno(clinica, 4)
    assert other_dentist_client.get(f"{URL}/config").json()["puede_editar"] is False
    assert other_dentist_client.put(f"{URL}/config", json=config_valida()).status_code == 403
    dueno(clinica, 99)
    assert other_dentist_client.get(f"{URL}/config").json()["puede_editar"] is True
    assert other_dentist_client.put(f"{URL}/config", json=config_valida()).status_code == 200


def test_la_recepcion_lee_y_no_edita(clinica, receptionist_client):
    assert receptionist_client.put(f"{URL}/config", json=config_valida()).status_code == 403


def test_si_no_se_sabe_quien_es_el_dueno_no_edita_nadie_mas_que_el_admin(clinica, other_dentist_client):
    dueno(clinica, 99)
    clinica.sql("DROP TABLE clinics")
    assert other_dentist_client.put(f"{URL}/config", json=config_valida()).status_code == 403


@pytest.mark.parametrize("metodo, ruta, cuerpo", [
    ("get", f"{URL}/config", None),
    ("put", f"{URL}/config", config_valida()),
    ("post", f"{URL}/config/vista-previa", {"cual": "control"}),
], ids=["ver", "guardar", "vista-previa"])
def test_un_paciente_no_entra_a_la_config(clinica, patient_client, metodo, ruta, cuerpo):
    r = getattr(patient_client, metodo)(ruta, **({"json": cuerpo} if cuerpo else {}))
    assert r.status_code == 403


def test_guardar_sin_csrf_no_pasa(clinica, client_sin_reraise):
    assert client_sin_reraise.put(f"{URL}/config", json=config_valida()).status_code == 403


# ---------------------------------------------------------------------------
# Qué acepta
# ---------------------------------------------------------------------------

def test_guarda_y_devuelve_lo_guardado(clinica, client):
    cuerpo = config_valida(textos={"control": "  Hola {nombre}, ¿venís a control?  ", "chicos": "   "},
                           dias_control=120, tope_diario=5, motivos_activos=["CONTROL"])
    r = client.put(f"{URL}/config", json=cuerpo)
    assert r.status_code == 200
    datos = client.get(f"{URL}/config").json()
    assert datos["textos"]["control"] == "Hola {nombre}, ¿venís a control?"
    assert datos["textos"]["chicos"] is None  # vacío = de fábrica
    assert (datos["dias_control"], datos["tope_diario"], datos["motivos_activos"]) == (120, 5, ["CONTROL"])
    assert datos["link_resena"] == "https://g.page/r/consultorio-sur/review"
    assert r.json() == datos


def test_lo_que_no_viene_vuelve_a_lo_de_fabrica(clinica, client):
    client.put(f"{URL}/config", json=config_valida(textos={"resena": "Hola {nombre}: {link_resena}"}, tope_diario=3))
    client.put(f"{URL}/config", json={})
    datos = client.get(f"{URL}/config").json()
    assert datos["textos"]["resena"] is None and datos["tope_diario"] == 10 and datos["link_resena"] is None


def test_apagar_los_dos_motivos_se_guarda(clinica, client):
    client.put(f"{URL}/config", json=config_valida(motivos_activos=[]))
    assert client.get(f"{URL}/config").json()["motivos_activos"] == []


@pytest.mark.parametrize("link", [
    "http://g.page/r/x/review", "javascript:alert(1)", "g.page/r/x", "https://con espacio.com", "https://",
    "https://" + "a" * 495 + ".com",
])
def test_un_link_que_no_es_https_da_422(clinica, client, link):
    r = client.put(f"{URL}/config", json=config_valida(link_resena=link))
    assert r.status_code == 422


def test_un_marcador_que_no_existe_da_422_y_dice_cual(clinica, client):
    r = client.put(f"{URL}/config", json=config_valida(textos={"presupuesto": "Hola {nombre} {apellido} {apellido} {}"}))
    assert r.status_code == 422
    assert "{apellido}, {}" in r.json()["detail"] and "presupuesto" in r.json()["detail"]


def test_un_texto_muy_largo_da_422(clinica, client):
    r = client.put(f"{URL}/config", json=config_valida(textos={"control": "a" * 1001}))
    assert r.status_code == 422


@pytest.mark.parametrize("campo, valor", [("dias_control", 89), ("dias_control", 731), ("tope_diario", 0), ("tope_diario", 31)])
def test_numeros_fuera_de_rango(clinica, client, campo, valor):
    assert client.put(f"{URL}/config", json=config_valida(**{campo: valor})).status_code == 422


# ---------------------------------------------------------------------------
# El panel usa la configuración
# ---------------------------------------------------------------------------

def test_el_panel_usa_el_texto_propio_con_sus_marcadores(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 245)
    client.put(f"{URL}/config", json=config_valida(
        textos={"control": "Hola {nombre}! Hace {meses} meses de tu visita a {clinica}. {profesional} te espera."}))
    assert hoy(client)["avisos"][0]["mensaje"] == "Hola Juan! Hace 8 meses de tu visita a Consultorio Sur. Analía Ríos te espera."


def test_los_chicos_usan_su_texto(clinica, client):
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    for hijo, nombre in [(21, "Tomás Gómez"), (22, "Sofía Gómez")]:
        clinica.usuario(hijo, nombre, None)
        clinica.a_cargo(hijo, 20)
        clinica.prestacion(hijo, 250)
    client.put(f"{URL}/config", json=config_valida(textos={"chicos": "{nombre}: control de {pacientes}"}))
    assert hoy(client)["avisos"][0]["mensaje"] == "Laura: control de Tomás y Sofía"


def test_el_presupuesto_usa_su_texto_con_la_fecha(clinica, client):
    clinica.usuario(11, "Carlos Díaz", "351 555-0011")
    clinica.presupuesto(30, 11, 12)
    client.put(f"{URL}/config", json=config_valida(textos={"presupuesto": "{nombre}, ¿y el presupuesto del {fecha}?"}))
    assert hoy(client)["avisos"][0]["mensaje"] == f"Carlos, ¿y el presupuesto del {hace(12):%d/%m}?"


def test_el_intervalo_de_control_de_la_clinica(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 130)
    assert hoy(client)["avisos"] == []
    client.put(f"{URL}/config", json=config_valida(dias_control=120))
    assert quienes(hoy(client)) == [("CONTROL", [10])]


def test_el_tope_de_la_clinica(clinica, client):
    for i in range(100, 106):
        clinica.usuario(i, f"P{i} X", f"351 555-{i:04d}")
        clinica.prestacion(i, 200 + i)
    client.put(f"{URL}/config", json=config_valida(tope_diario=2))
    r = hoy(client)
    assert len(r["avisos"]) == 2 and r["tope"] == 2


def test_un_motivo_apagado_no_se_sugiere_ni_figura_como_error(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.prestacion(10, 200)
    clinica.usuario(11, "Carlos Díaz", "351 555-0011")
    clinica.presupuesto(30, 11, 12)
    client.put(f"{URL}/config", json=config_valida(motivos_activos=["CONTROL"]))
    r = hoy(client)
    assert quienes(r) == [("CONTROL", [10])] and r["motivos_con_error"] == []
    client.put(f"{URL}/config", json=config_valida(motivos_activos=["PRESUPUESTO"]))
    assert quienes(hoy(client)) == [("PRESUPUESTO", [11])]


# ---------------------------------------------------------------------------
# El mensaje de «Pedir reseña»
# ---------------------------------------------------------------------------

def resenas(cliente, ids):
    r = cliente.get(f"{URL}/resenas", params={"patient_ids": ids})
    assert r.status_code == 200, r.text
    return r.json()


def test_sin_link_no_hay_mensaje(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    r = resenas(client, "10")
    assert r["link_configurado"] is False and r["pacientes"]["10"]["mensaje"] is None


def test_con_link_trae_el_mensaje_armado_y_el_numero(clinica, client):
    clinica.usuario(10, "JUAN PÉREZ", "351 555-0010")
    clinica.prestacion(10, 20)
    client.put(f"{URL}/config", json=config_valida())
    r = resenas(client, "10")
    paciente = r["pacientes"]["10"]
    assert r["link_configurado"] is True
    assert paciente["numero_whatsapp"] == "5493515550010"
    assert paciente["contacto"] == {"es_tutor": False, "nombre": "JUAN PÉREZ", "telefono": "351 555-0010"}
    assert paciente["mensaje"] == (
        "Hola Juan, ¿cómo estás? Soy Analía Ríos, de Consultorio Sur. Gracias por venir. Si te quedaste "
        "conforme con la atención, nos ayudaría mucho que nos dejes una reseña en Google: "
        "https://g.page/r/consultorio-sur/review")


def test_la_resena_de_un_chico_va_al_tutor(clinica, client):
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    clinica.usuario(21, "Tomás Gómez", None)
    clinica.a_cargo(21, 20)
    client.put(f"{URL}/config", json=config_valida())
    paciente = resenas(client, "21")["pacientes"]["21"]
    assert paciente["numero_whatsapp"] == "5493515550020"
    assert paciente["mensaje"].startswith("Hola Laura, ¿cómo estás? Te escribimos de Consultorio Sur. Gracias por traer a Tomás.")


def test_la_resena_con_texto_propio(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    client.put(f"{URL}/config", json=config_valida(textos={"resena": "{nombre}, dejanos tu reseña: {link_resena}"}))
    assert resenas(client, "10")["pacientes"]["10"]["mensaje"] == "Juan, dejanos tu reseña: https://g.page/r/consultorio-sur/review"


def test_sin_telefono_viene_el_mensaje_sin_numero(clinica, client):
    clinica.usuario(10, "Juan Pérez", "4555-1234")
    client.put(f"{URL}/config", json=config_valida())
    paciente = resenas(client, "10")["pacientes"]["10"]
    assert paciente["numero_whatsapp"] is None and paciente["mensaje"]


def test_de_otra_clinica_o_dado_de_baja_no_hay_mensaje(clinica, client):
    clinica.usuario(10, "Juan Pérez", "351 555-0010", baja=True)
    clinica.usuario(90, "De Otra", "351 555-0090", clinic_id=2)
    client.put(f"{URL}/config", json=config_valida())
    r = resenas(client, "10,90")
    assert r["pacientes"]["10"]["mensaje"] is None and r["pacientes"]["90"]["mensaje"] is None


def test_si_los_pacientes_no_se_pueden_leer_vienen_los_contadores(clinica, client):
    client.put(f"{URL}/config", json=config_valida())
    clinica.sql("DROP TABLE users")
    r = resenas(client, "10")
    assert r["link_configurado"] is True and r["pacientes"]["10"]["veces"] == 0 and r["pacientes"]["10"]["mensaje"] is None


def test_la_resena_lee_solo_las_prestaciones_de_esos_pacientes(clinica):
    clinica.prestacion(10, 20)
    clinica.prestacion(11, 20, profesional=9)
    assert contactos_hoy.leer_ultimas_prestaciones(1, HOY, {11}) == {11: (hace(20), 9)}
    assert contactos_hoy.leer_ultimas_prestaciones(1, HOY, set()) == {}


# ---------------------------------------------------------------------------
# La vista previa
# ---------------------------------------------------------------------------

def vista(cliente, **cuerpo):
    return cliente.post(f"{URL}/config/vista-previa", json=cuerpo)


def test_la_vista_previa_de_fabrica_con_el_que_pregunta(clinica, other_dentist_client):
    clinica.usuario(99, "Pedro Odontólogo", rol="dentist")
    r = vista(other_dentist_client, cual="control")
    assert r.status_code == 200
    assert r.json()["mensaje"].startswith("Hola Juan, ¿cómo estás? Soy Pedro Odontólogo, de Consultorio Sur. Ya pasaron 7 meses")


def test_la_vista_previa_de_un_texto_propio(clinica, receptionist_client):
    r = vista(receptionist_client, cual="chicos", texto="{nombre}: control de {pacientes} ({profesional})")
    assert r.json()["mensaje"] == "Laura: control de Tomás y Sofía ()"


def test_la_vista_previa_de_la_resena_con_el_link_que_se_esta_escribiendo(clinica, receptionist_client):
    r = vista(receptionist_client, cual="resena", link_resena="https://g.page/r/nuevo/review")
    assert r.json()["mensaje"].endswith("https://g.page/r/nuevo/review")
    r = vista(receptionist_client, cual="resena")
    assert r.json()["mensaje"].endswith("https://g.page/r/tu-clinica/review")


def test_la_vista_previa_rechaza_un_marcador_que_no_existe(clinica, receptionist_client):
    assert vista(receptionist_client, cual="presupuesto", texto="{monto}").status_code == 422


def test_la_vista_previa_no_guarda(clinica, client):
    vista(client, cual="control", texto="{nombre}")
    assert client.get(f"{URL}/config").json()["textos"]["control"] is None


# ---------------------------------------------------------------------------
# Lo de fábrica y la tabla
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cual, motivo, es_tutor, pacientes, extra", [
    ("control", "CONTROL", False, ["Juan"], {}),
    ("chicos", "CONTROL", True, ["Tomás"], {}),
    ("presupuesto", "PRESUPUESTO", False, ["Carlos"], {"fecha_presupuesto": date(2027, 3, 3)}),
    ("resena", "RESENA", False, ["María"], {}),
])
def test_los_textos_de_fabrica_dicen_lo_mismo_que_el_armador(cual, motivo, es_tutor, pacientes, extra):
    """Lo que muestra la configuración como «de fábrica» es lo que sale, cuando no falta ningún dato."""
    cfg = contactos_config.Config(link_resena="https://g.page/r/x/review")
    armado = contactos_hoy.armar_mensaje(cfg, motivo, "Ana López", es_tutor, pacientes, "Analía Ríos", "Sur",
                                         meses=7, **extra)
    plantilla = contactos_config.completar(contactos_config.DE_FABRICA[cual], {
        "nombre": "Ana", "pacientes": pacientes[0], "meses": 7, "fecha": "03/03", "clinica": "Sur",
        "profesional": "Analía Ríos", "link_resena": "https://g.page/r/x/review"})
    assert armado == plantilla


@pytest.mark.parametrize("cual", contactos_config.TEXTOS)
def test_los_textos_de_fabrica_no_dicen_cita_ni_traen_marcadores_raros(cual):
    texto = contactos_config.DE_FABRICA[cual]
    assert "cita" not in texto.lower() and contactos_config.marcadores_desconocidos(texto) == []


def test_completar_emprolija_lo_que_falta():
    assert contactos_config.completar("Hola {nombre} , soy {profesional}.", {"nombre": "Ana"}) == "Hola Ana, soy."


def test_la_migracion_crea_la_tabla_que_declara_el_modelo():
    import importlib.util
    import pathlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ruta = pathlib.Path(__file__).parent.parent / "alembic" / "versions" / "c7e3a1f5b8d2_contact_settings.py"
    spec = importlib.util.spec_from_file_location("migracion_558", ruta)
    migracion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracion)
    assert migracion.down_revision == "b4d8e2f6a9c1"

    otra = create_engine("sqlite://")
    with otra.begin() as cx:
        with Operations.context(MigrationContext.configure(cx)):
            migracion.upgrade()
        de_la_migracion = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(cx).get_columns("contact_settings")}
    with engine_de_turnos.connect() as cx:
        del_modelo = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspect(cx).get_columns("contact_settings")}
    assert de_la_migracion == del_modelo


# ---------------------------------------------------------------------------
# Lo que trajo la revisión independiente del #558
# ---------------------------------------------------------------------------

def test_resenas_no_deja_avisos_de_pydantic_con_telefonos(clinica, client):
    """Un dict asignado al contacto hacía que Pydantic avisara con el teléfono adentro, al log."""
    import warnings
    clinica.usuario(10, "Juan Pérez", "351 555-0010")
    clinica.usuario(20, "Laura Gómez", "351 555-0020")
    clinica.usuario(21, "Tomás Gómez", None)
    clinica.a_cargo(21, 20)
    client.put(f"{URL}/config", json=config_valida())
    with warnings.catch_warnings(record=True) as avisos:
        warnings.simplefilter("always")
        resenas(client, "10,21")
    pydantic = [str(a.message) for a in avisos if "serializ" in str(a.message).lower()]
    assert pydantic == []


@pytest.mark.parametrize("cual", contactos_config.TEXTOS)
def test_un_texto_igual_al_de_fabrica_se_guarda_como_de_fabrica(clinica, client, cual):
    """«Partir del de fábrica» y guardar sin tocar nada no tiene que dejar un texto que no se acomoda."""
    texto = "  " + contactos_config.DE_FABRICA[cual].replace(" ", "  ", 2) + "\n"
    client.put(f"{URL}/config", json=config_valida(textos={cual: texto}))
    assert client.get(f"{URL}/config").json()["textos"][cual] is None


def test_un_texto_de_fabrica_con_un_cambio_si_se_guarda(clinica, client):
    texto = contactos_config.DE_FABRICA["control"] + " ¡Te esperamos!"
    client.put(f"{URL}/config", json=config_valida(textos={"control": texto}))
    assert client.get(f"{URL}/config").json()["textos"]["control"] == texto


def test_la_vista_previa_del_texto_de_fabrica_se_acomoda_como_el_de_fabrica(clinica, receptionist_client):
    """Sin profesional (la recepción) dice «Te escribimos de…», no «Soy , de…»."""
    r = vista(receptionist_client, cual="control", texto=contactos_config.DE_FABRICA["control"])
    assert "Te escribimos de Consultorio Sur." in r.json()["mensaje"]


def test_dos_que_guardan_a_la_vez_la_primera_vez_no_dan_500(clinica, monkeypatch):
    """El segundo choca con la clave primaria y escribe sobre la fila del primero."""
    from models import ContactSettings
    db = TestingSessionLocal()
    otra = TestingSessionLocal()
    otra.add(ContactSettings(clinic_id=1, tope_diario=3))
    otra.commit()
    otra.close()
    original = db.get
    vistas = []

    def get_que_llega_tarde(modelo, clave):
        vistas.append(clave)
        return None if len(vistas) == 1 else original(modelo, clave)

    monkeypatch.setattr(db, "get", get_que_llega_tarde)
    contactos_config.guardar(db, 1, contactos_config.Config(tope_diario=7))
    db.close()
    db = TestingSessionLocal()
    assert db.get(ContactSettings, 1).tope_diario == 7
    db.close()
