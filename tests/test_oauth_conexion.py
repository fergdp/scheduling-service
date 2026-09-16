"""
Conectar y desconectar Google Calendar, que es lo que el odontólogo ve como un botón.

Faltaban los tres caminos que se tocan desde la pantalla de Turnos: consultar si está
conectado, desconectar, y el HTML que devuelve el popup al volver de Google.

El de desconectar importa especialmente: es un botón de un solo click, irreversible —
recuperar la conexión es rehacer el permiso en Google, con su pantalla de advertencia — y
antes no tenía ningún test.
"""
import pytest

from models import DentistCalendarConfig

BASE = "/clinic-scheduling-api/v1/oauth"


def _conectar_en_db(dentist_user_id=1, clinic_id=1, email="dentista@gmail.com"):
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    db.add(DentistCalendarConfig(
        dentist_user_id=dentist_user_id, clinic_id=clinic_id, google_email=email,
        google_access_token="acceso", google_refresh_token="refresh", sync_enabled=True,
    ))
    db.commit()
    db.close()


def _config(dentist_user_id=1):
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    try:
        return db.query(DentistCalendarConfig).filter(
            DentistCalendarConfig.dentist_user_id == dentist_user_id).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Estado de la conexión
# ---------------------------------------------------------------------------

def test_sin_conectar_el_estado_dice_que_no(client):
    res = client.get(f"{BASE}/status")
    assert res.status_code == 200, res.text
    assert res.json() == {"connected": False, "email": None, "sync_enabled": False}


def test_conectado_el_estado_trae_el_mail(client):
    _conectar_en_db()
    res = client.get(f"{BASE}/status")

    assert res.status_code == 200, res.text
    assert res.json() == {"connected": True, "email": "dentista@gmail.com", "sync_enabled": True}


def test_una_config_sin_refresh_token_no_cuenta_como_conectada(client):
    """
    Es el estado en el que queda una conexión a medias. Decir "conectado" ahí haría que la
    recepcionista cargue turnos esperando que lleguen al celular del odontólogo, y no llegan.
    """
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    db.add(DentistCalendarConfig(dentist_user_id=1, clinic_id=1, google_email="a@b.com"))
    db.commit()
    db.close()

    assert client.get(f"{BASE}/status").json()["connected"] is False


def test_el_paciente_no_puede_ver_el_estado(patient_client):
    assert patient_client.get(f"{BASE}/status").status_code == 403


# ---------------------------------------------------------------------------
# Desconectar
# ---------------------------------------------------------------------------

def test_desconectar_borra_las_credenciales_de_google(client):
    """
    Tienen que quedar en nulo las cuatro: si sobrevive el refresh token, el sistema sigue
    escribiendo en el calendario de alguien que pidió desconectarse.
    """
    _conectar_en_db()

    res = client.delete(f"{BASE}/disconnect")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "disconnected"}

    config = _config()
    assert config.google_refresh_token is None
    assert config.google_access_token is None
    assert config.token_expiry is None
    assert config.google_email is None
    assert config.sync_enabled is False


def test_desconectar_sin_estar_conectado_no_rompe(client):
    """El botón puede llegar a apretarse dos veces; la segunda no puede dar 500."""
    res = client.delete(f"{BASE}/disconnect")
    assert res.status_code == 200, res.text


def test_el_paciente_no_puede_desconectar_a_nadie(patient_client):
    _conectar_en_db()
    assert patient_client.delete(f"{BASE}/disconnect").status_code == 403
    assert _config().google_refresh_token is not None, "le borró las credenciales igual"


def test_desconectar_no_toca_la_conexion_de_otro_odontologo(client):
    """El borrado está scopeado al que pide: no puede arrastrar a un colega."""
    _conectar_en_db(dentist_user_id=1)
    _conectar_en_db(dentist_user_id=2)

    assert client.delete(f"{BASE}/disconnect").status_code == 200

    assert _config(1).google_refresh_token is None
    assert _config(2).google_refresh_token == "refresh", "desconectó al odontólogo 2"


# ---------------------------------------------------------------------------
# El HTML que cierra el popup
# ---------------------------------------------------------------------------

def test_el_mail_va_escapado_en_el_html_y_en_el_javascript(client, monkeypatch):
    """
    El mail lo devuelve Google, no el usuario, pero entra a un HTML y adentro de un string de
    JavaScript. Sin escapar, un valor con comillas o con `<` rompe la página o inyecta código.

    El `state` firmado se obtiene pidiéndole la URL de autorización al propio servicio, que es
    como lo consigue el navegador: no hay forma de fabricarlo por fuera sin copiar la firma.
    """
    import routers.oauth as ro
    from test_oauth import _get_valid_state

    monkeypatch.setattr(ro, "exchange_code_for_tokens", lambda code: {
        "access_token": "acceso", "refresh_token": "refresh", "token_expiry": None,
    })
    peligroso = "<script>alerta</script>'\"@b.com"
    monkeypatch.setattr(ro, "get_google_user_email", lambda *a, **k: peligroso)

    state, _ = _get_valid_state(client)
    res = client.get(f"{BASE}/callback", params={"code": "x", "state": state})

    assert res.status_code == 200, res.text
    assert peligroso not in res.text, "el mail entró crudo: se puede inyectar HTML o JavaScript"
    assert "&lt;script&gt;" in res.text, "no quedó escapado en el HTML"
    assert "\\u003cscript\\u003e" in res.text or '\\"' in res.text, "no quedó escapado en el JS"
    assert ",'*')" not in res.text, "el aviso sigue saliendo a cualquier ventana"


def test_el_aviso_al_front_no_va_a_cualquier_ventana():
    """
    `postMessage(..., '*')` le entrega el mail del odontólogo a cualquier página que haya
    abierto el popup. El destino tiene que ser el origen del front.
    """
    import os
    from routers.oauth import _origen_del_front

    anterior = os.environ.get("CORS_ORIGINS")
    os.environ["CORS_ORIGINS"] = "https://atuconsul.com,https://www.atuconsul.com"
    try:
        assert _origen_del_front() == "https://atuconsul.com"
    finally:
        if anterior is None:
            os.environ.pop("CORS_ORIGINS", None)
        else:
            os.environ["CORS_ORIGINS"] = anterior


def test_sin_origenes_configurados_cae_al_dominio_propio():
    """Nunca `'*'`: sin configuración, el destino es el sitio de producción."""
    import os
    from routers.oauth import _origen_del_front

    anterior = os.environ.get("CORS_ORIGINS")
    os.environ["CORS_ORIGINS"] = ""
    try:
        assert _origen_del_front() == "https://atuconsul.com"
    finally:
        if anterior is None:
            os.environ.pop("CORS_ORIGINS", None)
        else:
            os.environ["CORS_ORIGINS"] = anterior
