"""
La puerta de entrada del servicio: leer el token y descifrar las credenciales de Google.

La auditoría encontró que **ningún test decodificaba un JWT de verdad**: todos pisan
`get_current_user` con `dependency_overrides`, así que la función que decide quién sos —y que
protege los siete endpoints de turnos— nunca se ejecutaba. Un cambio que la rompiera, o que
dejara pasar un token con firma inválida, salía con la suite entera en verde.

Lo mismo con el cifrado de los tokens de Google: se guardan cifrados en la base y el camino de
descifrado sólo se ejercitaba a través de mocks.
"""
import base64
import os

import pytest
from datetime import datetime, timedelta, timezone
from jose import jwt


def _token(payload=None, clave=None, algoritmo="HS256"):
    """Firma un JWT con la misma clave y algoritmo que usa el servicio."""
    from dependencies import ALGORITHM, SECRET_KEY_BYTES
    cuerpo = {
        "user_id": 1, "clinic_id": 1, "roles": ["ROLE_ADMIN"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    cuerpo.update(payload or {})
    return jwt.encode(cuerpo, clave or SECRET_KEY_BYTES, algorithm=algoritmo or ALGORITHM)


# ---------------------------------------------------------------------------
# Leer el token
# ---------------------------------------------------------------------------

def test_lee_el_token_de_la_cookie():
    """El camino real: el JWT viaja en una cookie httpOnly, no en un header."""
    from dependencies import get_current_user

    payload = get_current_user(token_cookie=_token(), credentials=None)

    assert payload is not None, "no pudo leer un token válido de la cookie"
    assert payload["user_id"] == 1
    assert payload["roles"] == ["ROLE_ADMIN"]


def test_tambien_acepta_el_header_bearer():
    """Lo usan las herramientas y los scripts, que no manejan cookies."""
    from fastapi.security import HTTPAuthorizationCredentials
    from dependencies import get_current_user

    credenciales = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_token())
    payload = get_current_user(token_cookie=None, credentials=credenciales)

    assert payload is not None
    assert payload["clinic_id"] == 1


def test_la_cookie_le_gana_al_header():
    """Fija cuál manda cuando llegan los dos, para que no dependa del orden del código."""
    from fastapi.security import HTTPAuthorizationCredentials
    from dependencies import get_current_user

    credenciales = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=_token({"user_id": 999}))
    payload = get_current_user(token_cookie=_token({"user_id": 1}), credentials=credenciales)

    assert payload["user_id"] == 1


def test_sin_token_no_hay_usuario():
    from dependencies import get_current_user
    assert get_current_user(token_cookie=None, credentials=None) is None


def test_un_token_firmado_con_otra_clave_no_sirve():
    """
    El control que importa: si esto pasara, cualquiera se firma su propio token con el rol que
    quiera y entra como administrador de cualquier clínica.
    """
    from dependencies import get_current_user

    otra_clave = base64.b64decode(base64.b64encode(b"una clave completamente distinta 1234"))
    ajeno = _token(clave=otra_clave)

    assert get_current_user(token_cookie=ajeno, credentials=None) is None


def test_un_token_vencido_no_sirve():
    from dependencies import get_current_user

    vencido = _token({"exp": datetime.now(timezone.utc) - timedelta(minutes=1)})

    assert get_current_user(token_cookie=vencido, credentials=None) is None


def test_un_token_que_no_es_un_token_no_rompe():
    from dependencies import get_current_user
    assert get_current_user(token_cookie="esto no es un jwt", credentials=None) is None


# ---------------------------------------------------------------------------
# Los roles que salen del token
# ---------------------------------------------------------------------------

def test_los_roles_pierden_el_prefijo_de_spring():
    """Spring emite `ROLE_ADMIN`; este servicio razona con `ADMIN`."""
    from dependencies import get_roles

    assert get_roles({"roles": ["ROLE_ADMIN", "ROLE_DENTIST"]}) == ["ADMIN", "DENTIST"]
    assert get_roles({"roles": ["receptionist"]}) == ["RECEPTIONIST"]


def test_sin_payload_no_hay_roles():
    """Un token ilegible no puede rendir como "sin rol pero autenticado"."""
    from dependencies import get_roles

    assert get_roles(None) == []
    assert get_roles({}) == []


# ---------------------------------------------------------------------------
# Las credenciales de Google se guardan cifradas
# ---------------------------------------------------------------------------

def test_el_token_de_google_va_y_vuelve_igual():
    from utils.crypto import decrypt_token, encrypt_token

    original = "1//0abcDEF-refresh-token-de-google"
    cifrado = encrypt_token(original)

    assert cifrado != original, "lo guardó en texto plano"
    assert decrypt_token(cifrado) == original


def test_dos_cifrados_del_mismo_token_dan_distinto():
    """
    Fernet le pone a cada cifrado su propio vector de inicialización: dos resultados iguales
    delatarían un vector fijo, que es lo que rompe el modo.
    """
    from utils.crypto import encrypt_token

    assert encrypt_token("mismo-token") != encrypt_token("mismo-token")


def test_los_campos_vacios_pasan_de_largo():
    """
    El odontólogo que nunca conectó Google tiene los campos en nulo, y el que se desconectó
    los tiene vacíos: ninguno de los dos casos puede explotar al leer la configuración.
    """
    from utils.crypto import decrypt_token, encrypt_token

    assert encrypt_token(None) is None
    assert decrypt_token(None) is None
    assert encrypt_token("") == ""
    assert decrypt_token("") == ""
