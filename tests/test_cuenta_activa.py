"""
Una cuenta deshabilitada o dada de baja pierde la sesión en este servicio (#547).

Hasta el #547 este servicio sólo miraba la firma y el vencimiento del JWT: a una recepcionista
dada de baja con la sesión abierta le seguía respondiendo hasta que vencía el token. La regla es
la de dental-clinic y la de clinical-history (`puedeEntrar`, #415): habilitada Y sin la marca de
baja. Acá se prueba con los guards de verdad (`get_current_user` sin override); la base de
dental-clinic se simula con la fila tal cual la devuelve pymysql (`is_enabled` BIT(1) -> bytes).
"""
import os

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import dependencies
from dependencies import get_clinic_id, get_current_user, get_user_id

HABILITADA = b"\x01"
DESHABILITADA = b"\x00"
UNA_FECHA = datetime(2026, 11, 10, 12, 0)


@pytest.fixture(autouse=True)
def cache_vacio():
    dependencies._user_clinic_cache.clear()
    yield
    dependencies._user_clinic_cache.clear()


def _token(**cambios) -> str:
    cuerpo = {
        "user_id": 7, "clinic_id": 1, "roles": ["ROLE_RECEPTIONIST"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    cuerpo.update(cambios)
    cuerpo = {k: v for k, v in cuerpo.items() if v is not None}
    return jwt.encode(cuerpo, dependencies.SECRET_KEY_BYTES, algorithm=dependencies.ALGORITHM)


def _base_con(fila):
    """La base de dental-clinic devuelve `fila` (o nada) para el usuario del token."""
    sesion = MagicMock()
    sesion.execute.return_value.first.return_value = fila
    return patch.object(dependencies, "ClinicSessionLocal", return_value=sesion)


# Una app mínima con los tres guards de los que cuelgan los endpoints del servicio: los que
# piden la clínica, los que sólo piden el usuario y los que sólo leen el token.
_app = FastAPI()


@_app.get("/con-clinica")
def _con_clinica(clinic_id: int = Depends(get_clinic_id)):
    return {"clinic_id": clinic_id}


@_app.get("/con-usuario")
def _con_usuario(user_id: int = Depends(get_user_id)):
    return {"user_id": user_id}


@_app.get("/con-token")
def _con_token(payload=Depends(get_current_user)):
    return {"hay_sesion": payload is not None}


_cliente = TestClient(_app)


def _pedir(ruta: str, fila):
    with _base_con(fila):
        return _cliente.get(ruta, cookies={"token": _token()})


# --- El corte, de punta a punta ---------------------------------------------------------------

@pytest.mark.parametrize("ruta", ["/con-clinica", "/con-usuario"])
def test_la_cuenta_habilitada_entra(ruta):
    assert _pedir(ruta, (1, HABILITADA, None)).status_code == 200


@pytest.mark.parametrize("ruta", ["/con-clinica", "/con-usuario"])
@pytest.mark.parametrize(
    "fila",
    [
        pytest.param((1, DESHABILITADA, None), id="deshabilitada"),
        pytest.param((1, DESHABILITADA, UNA_FECHA), id="dada-de-baja"),
        # #485: un dado de baja puede quedar con is_enabled = 1. Vale la marca igual.
        pytest.param((1, HABILITADA, UNA_FECHA), id="dada-de-baja-con-is_enabled-prendido"),
        pytest.param((1, None, None), id="is_enabled-vacio"),
    ],
)
def test_la_cuenta_que_no_puede_entrar_es_401(ruta, fila):
    """401 y no 403: volver a entrar es lo que corresponde (el #264 deja el 403 para el que
    no tiene clínica)."""
    assert _pedir(ruta, fila).status_code == 401


def test_sin_sesion_tambien_para_lo_que_solo_lee_el_token():
    assert _pedir("/con-token", (1, DESHABILITADA, UNA_FECHA)).json() == {"hay_sesion": False}
    dependencies._user_clinic_cache.clear()
    assert _pedir("/con-token", (1, HABILITADA, None)).json() == {"hay_sesion": True}


def test_el_usuario_que_ya_no_esta_en_la_base_no_tiene_sesion():
    assert _pedir("/con-usuario", None).status_code == 401


def test_un_token_sin_user_id_no_tiene_sesion():
    with _base_con((1, HABILITADA, None)):
        r = _cliente.get("/con-token", cookies={"token": _token(user_id=None)})
    assert r.json() == {"hay_sesion": False}


def test_la_cuenta_reactivada_vuelve_a_entrar_cuando_vence_el_cache():
    assert _pedir("/con-usuario", (1, DESHABILITADA, UNA_FECHA)).status_code == 401
    # Mientras dura el cache, la base dice otra cosa y no se la consulta.
    assert _pedir("/con-usuario", (1, HABILITADA, None)).status_code == 401
    dependencies._user_clinic_cache.clear()  # = pasó el TTL
    assert _pedir("/con-usuario", (1, HABILITADA, None)).status_code == 200


def test_la_ventana_del_cache_es_de_un_minuto():
    """Es lo que tarda, como mucho, en cortarse la sesión de una baja (#547)."""
    assert dependencies._user_clinic_cache.ttl == 60


def test_una_sola_consulta_para_la_sesion_y_la_clinica():
    with _base_con((1, HABILITADA, None)) as fabrica:
        r = _cliente.get("/con-clinica", cookies={"token": _token()})
    assert r.status_code == 200
    assert fabrica.call_count == 1
    sql = str(fabrica.return_value.execute.call_args.args[0])
    assert "is_enabled" in sql and "deactivated_at" in sql and "clinic_id" in sql


# --- La regla -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "valor, esperado",
    [
        # pymysql devuelve BIT(1) como bytes, y b"\x00" es VERDADERO en Python.
        (b"\x00", False), (b"\x01", True),
        (0, False), (1, True), (False, False), (True, True),
        (None, False),
    ],
)
def test_is_enabled_se_lee_bien_venga_como_venga(valor, esperado):
    assert dependencies._bit_verdadero(valor) is esperado


@pytest.mark.parametrize(
    "is_enabled, deactivated_at, esperado",
    [
        (HABILITADA, None, True),
        (DESHABILITADA, None, False),
        (HABILITADA, UNA_FECHA, False),
        (DESHABILITADA, UNA_FECHA, False),
        (None, None, False),
    ],
)
def test_puede_entrar_es_la_regla_de_dental_clinic(is_enabled, deactivated_at, esperado):
    fila = dependencies.FilaDeUsuario(1, is_enabled, deactivated_at)
    assert dependencies.puede_entrar(fila) is esperado


# --- Lo que encontró el review ----------------------------------------------------------------

def test_si_la_base_no_responde_es_503_y_no_se_cachea():
    """Falla cerrada pero legible: también es lo que se ve si falta el GRANT de una columna."""
    from sqlalchemy.exc import OperationalError
    sesion = MagicMock()
    sesion.execute.side_effect = OperationalError("SELECT", {}, Exception("caída"))
    with patch.object(dependencies, "ClinicSessionLocal", return_value=sesion):
        r = _cliente.get("/con-token", cookies={"token": _token()})
    assert r.status_code == 503
    assert len(dependencies._user_clinic_cache) == 0
    sesion.close.assert_called_once()


def test_un_user_id_que_no_es_un_numero_no_tiene_sesion():
    with _base_con((1, HABILITADA, None)):
        r = _cliente.get("/con-token", cookies={"token": _token(user_id="abc")})
    assert r.json() == {"hay_sesion": False}


# Como las rutas de verdad: el guard de rol en `dependencies=[...]` corre ANTES que
# `get_clinic_id`. Sin el 401 propio de `require_any_role`, la cuenta cortada recibía 403 y el
# front no la mandaba al login.
from dependencies import require_any_role  # noqa: E402


@_app.get("/rol-primero", dependencies=[require_any_role("RECEPTIONIST", "DENTIST", "ADMIN")])
def _rol_primero(clinic_id: int = Depends(get_clinic_id)):
    return {"clinic_id": clinic_id}


def test_con_el_guard_de_rol_primero_la_cuenta_cortada_tambien_es_401():
    assert _pedir("/rol-primero", (1, DESHABILITADA, UNA_FECHA)).status_code == 401
    dependencies._user_clinic_cache.clear()
    assert _pedir("/rol-primero", (1, HABILITADA, None)).status_code == 200
