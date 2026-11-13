"""
El JWT se firma y verifica con PyJWT desde el #475 (2026-11-12); antes, python-jose 3.3.0.

`test_token_y_cifrado.py` ya cubre cookie, header, otra clave, vencido y basura en
`get_current_user`. Esto suma lo que la librería podría cambiar sin que esos tests se enteren:
el algoritmo fijo en HS256 (ni otro HMAC ni `none`), la forma exacta del token de dental-clinic,
los dos lugares de `main.py` que decodifican a mano (`log_requests` y el `exception_handler`) y
el `state` de OAuth, que este servicio además FIRMA.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from dependencies import SECRET_KEY_BYTES, _payload_de
from routers import oauth

_OTRA_CLAVE = b"otra-clave-que-no-es-la-del-sistema-0123456789"


def _token(payload=None, clave=None, algoritmo="HS256"):
    cuerpo = {"sub": "dentista", "user_id": 5, "clinic_id": 1, "roles": ["ROLE_DENTIST"],
              "iat": int(time.time()), "exp": int(time.time()) + 600}
    cuerpo.update(payload or {})
    return jwt.encode(cuerpo, clave if clave is not None else SECRET_KEY_BYTES, algorithm=algoritmo)


def _vencido():
    return _token({"exp": int(time.time()) - 10})


# --- dependencies._payload_de ---------------------------------------------------------------

@pytest.mark.parametrize("algoritmo", ["HS512", "HS384"])
def test_otro_algoritmo_aunque_sea_con_la_misma_clave_no_sirve(algoritmo):
    """El algoritmo está fijo en HS256: es lo que cierra la confusión de algoritmos."""
    assert _payload_de(_token(algoritmo=algoritmo)) is None


def test_un_token_sin_firma_no_sirve():
    assert _payload_de(jwt.encode({"user_id": 5, "clinic_id": 1}, None, algorithm="none")) is None


def test_un_token_con_la_forma_exacta_de_dental_clinic_pasa():
    """Lo que emite `JwtUtil.java` de dental-clinic: `sub` = username, `iat`/`exp` enteros,
    `clinic_id`, `user_id` (que el constructor viejo deja en null), `roles` y la huella `pwd`."""
    ahora = int(time.time())
    payload = {"sub": "recepcion.ana", "iat": ahora, "exp": ahora + 3600, "clinic_id": 7,
               "user_id": None, "roles": ["ROLE_RECEPTIONIST"], "pwd": "a1b2c3d4"}
    assert _payload_de(jwt.encode(payload, SECRET_KEY_BYTES, algorithm="HS256")) == payload


@pytest.mark.parametrize("basura", ["fake.jwt.token", "no-es-un-jwt", "a.b", "...", "a.b.c.d"])
def test_basura_no_sirve_y_no_explota(basura):
    assert _payload_de(basura) is None


# --- main.log_requests y main.exception_handler ---------------------------------------------

_MALOS = {"vencido": _vencido, "otra_clave": lambda: _token(clave=_OTRA_CLAVE), "basura": lambda: "a.b.c.d"}
_RUTA = "/clinic-scheduling-api/v1/appointments/upcoming"


def _mensajes(caplog, texto):
    return "\n".join(r.getMessage() for r in caplog.records if texto in r.getMessage())


@pytest.mark.parametrize("malo", list(_MALOS))
def test_log_requests_marca_invalid_token_y_no_explota(client, caplog, malo):
    client.cookies.set("token", _MALOS[malo]())
    with caplog.at_level(logging.INFO):
        res = client.get(_RUTA)
    assert res.status_code == 200, "el middleware no puede romper el pedido"
    assert "from user: invalid_token" in _mensajes(caplog, "Incoming request")


@pytest.mark.parametrize("malo", list(_MALOS))
def test_exception_handler_marca_invalid_token(client_sin_reraise, caplog, monkeypatch, malo):
    import routers.appointments as ra

    def _explota(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ra, "_utcnow_naive", _explota)

    client_sin_reraise.cookies.set("token", _MALOS[malo]())
    with caplog.at_level(logging.INFO):
        res = client_sin_reraise.get(_RUTA)
    assert res.status_code == 500
    assert "from user: invalid_token" in _mensajes(caplog, "Unhandled exception")


# --- routers.oauth: el state que firma este servicio ---------------------------------------

def test_el_state_de_oauth_va_y_vuelve():
    datos = oauth._decode_state_jwt(oauth._make_state_jwt(user_id=5, clinic_id=1))
    assert (datos["user_id"], datos["clinic_id"]) == (5, 1)
    assert datos["exp"] > time.time()


def test_el_state_sale_firmado_en_hs256():
    assert jwt.get_unverified_header(oauth._make_state_jwt(user_id=5, clinic_id=1))["alg"] == "HS256"


@pytest.mark.parametrize("state", [
    jwt.encode({"user_id": 5, "clinic_id": 1, "exp": datetime.now(timezone.utc) - timedelta(seconds=10)},
               SECRET_KEY_BYTES, algorithm="HS256"),
    jwt.encode({"user_id": 5, "clinic_id": 1}, _OTRA_CLAVE, algorithm="HS256"),
    jwt.encode({"user_id": 5, "clinic_id": 1}, SECRET_KEY_BYTES, algorithm="HS512"),
    "a.b.c.d",
], ids=["vencido", "otra_clave", "otro_algoritmo", "basura"])
def test_un_state_invalido_es_400(state):
    with pytest.raises(HTTPException) as e:
        oauth._decode_state_jwt(state)
    assert e.value.status_code == 400
