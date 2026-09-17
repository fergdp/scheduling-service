"""
El rate limiter de la agenda agrupa por usuario, no por IP (issue #303).

`GET /v1/appointments/` (y el resto de las rutas de turnos y lista de espera, que comparten el
mismo `Limiter`) estaba limitado por `get_remote_address`. Todo el personal de una clínica sale
a internet por el mismo router, así que el tope de 60/min era, en los hechos, un tope por
CLÍNICA ENTERA: una recepcionista y varios odontólogos mirando la agenda a la vez lo agotaban, y
el backend respondía 429 — la agenda se veía vacía con «Error al cargar los turnos». Se vio en
los propios E2E: la corrida completa pasaba el tope y los tests fallaban buscando turnos que
existían.

Como en `test_token_y_cifrado.py`: los tests de endpoints pisan `get_current_user` con
`dependency_overrides`, así que `key_por_usuario_o_ip` —que lee el `Request` crudo, ANTES de que
FastAPI resuelva esa dependencia— nunca se ejercitaría con un mock. Se prueba con un JWT de
verdad, firmado con la misma clave que usa el servicio.
"""
from datetime import datetime, timedelta, timezone

from jose import jwt
from starlette.requests import Request

import dependencies
from dependencies import ALGORITHM, SECRET_KEY_BYTES, key_por_usuario_o_ip
from routers.appointments import limiter as apts_limiter
from test_agenda_recepcion import BASE


def _token(user_id=1, **extra):
    """Firma un JWT con la misma clave y algoritmo que usa el servicio (test_token_y_cifrado.py)."""
    cuerpo = {
        "user_id": user_id, "clinic_id": 1, "roles": ["ROLE_DENTIST"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    cuerpo.update(extra)
    return jwt.encode(cuerpo, SECRET_KEY_BYTES, algorithm=ALGORITHM)


def _request(cookie_token=None, auth_header=None, ip="203.0.113.5"):
    """Un `Request` de Starlette de verdad, con la cookie o el header tal como llegan por HTTP."""
    headers = []
    if cookie_token:
        headers.append((b"cookie", f"token={cookie_token}".encode()))
    if auth_header:
        headers.append((b"authorization", auth_header.encode()))
    return Request({"type": "http", "headers": headers, "client": (ip, 12345)})


# ---------------------------------------------------------------------------
# key_por_usuario_o_ip — la clave que arma el limiter
# ---------------------------------------------------------------------------

def test_dos_usuarios_de_la_misma_ip_dan_claves_distintas():
    """El caso del issue: una recepcionista y un odontólogo, mismo router de la clínica."""
    clave_a = key_por_usuario_o_ip(_request(cookie_token=_token(user_id=1), ip="203.0.113.5"))
    clave_b = key_por_usuario_o_ip(_request(cookie_token=_token(user_id=2), ip="203.0.113.5"))
    assert clave_a == "user:1"
    assert clave_b == "user:2"
    assert clave_a != clave_b


def test_el_mismo_usuario_da_la_misma_clave_aunque_cambie_la_ip():
    """El celular de recepción por WiFi y por datos: sigue siendo la misma persona."""
    por_wifi = key_por_usuario_o_ip(_request(cookie_token=_token(user_id=7), ip="203.0.113.5"))
    por_datos = key_por_usuario_o_ip(_request(cookie_token=_token(user_id=7), ip="198.51.100.9"))
    assert por_wifi == por_datos == "user:7"


def test_acepta_el_token_del_header_bearer():
    """Lo usan las herramientas y los scripts, que no manejan cookies (igual que get_current_user)."""
    clave = key_por_usuario_o_ip(_request(auth_header=f"Bearer {_token(user_id=3)}"))
    assert clave == "user:3"


def test_la_cookie_le_gana_al_header():
    clave = key_por_usuario_o_ip(_request(
        cookie_token=_token(user_id=1), auth_header=f"Bearer {_token(user_id=2)}",
    ))
    assert clave == "user:1"


def test_sin_sesion_cae_a_la_ip():
    """Rutas públicas, o un pedido sin JWT que igual llega al endpoint: ahí sí hace falta la IP."""
    clave_a = key_por_usuario_o_ip(_request(ip="203.0.113.5"))
    clave_b = key_por_usuario_o_ip(_request(ip="198.51.100.9"))
    assert clave_a == "203.0.113.5"
    assert clave_b == "198.51.100.9"


def test_un_token_invalido_cae_a_la_ip_no_rompe():
    clave = key_por_usuario_o_ip(_request(cookie_token="esto-no-es-un-jwt", ip="203.0.113.5"))
    assert clave == "203.0.113.5"


def test_un_token_sin_user_id_cae_a_la_ip():
    sin_user_id = jwt.encode({"clinic_id": 1}, SECRET_KEY_BYTES, algorithm=ALGORITHM)
    clave = key_por_usuario_o_ip(_request(cookie_token=sin_user_id, ip="203.0.113.5"))
    assert clave == "203.0.113.5"


def test_el_limiter_de_turnos_queda_armado_con_esta_clave():
    """
    Regresión directa: que alguien vuelva a poner `Limiter(key_func=get_remote_address)` no
    puede pasar en silencio. `routers/waitlist.py` importa este mismo `limiter`, así que
    queda cubierto sin un test propio.
    """
    assert apts_limiter._key_func is key_por_usuario_o_ip


# ---------------------------------------------------------------------------
# Extremo a extremo: el endpoint real, con dos JWT reales
# ---------------------------------------------------------------------------

def test_dos_usuarios_de_la_misma_ip_no_comparten_el_cupo(client):
    """
    `client` (dentist/admin, user_id=1) ya resuelve `get_db`/`get_clinic_id` por su cuenta —eso
    no es lo que se prueba acá—; sólo se le planta encima la cookie real que el limiter sí lee
    directo del `Request`, para simular a dos personas distintas entrando por la MISMA IP (el
    mismo `TestClient`, que es exactamente lo que reproduce «toda la clínica por el mismo
    router»).

    Antes de #303 esto agotaba las 60 para cualquiera que compartiera esa IP; ahora cada `user:`
    tiene su propio cupo.
    """
    client.cookies.set("token", _token(user_id=201))
    respuestas = [client.get(f"{BASE}/upcoming") for _ in range(61)]
    assert all(r.status_code == 200 for r in respuestas[:60]), \
        "el propio usuario 201 no debería frenar antes del pedido 61"
    assert respuestas[60].status_code == 429, "el propio usuario 201 sí tiene que frenar en el 61"

    client.cookies.set("token", _token(user_id=202))
    respuesta_otro = client.get(f"{BASE}/upcoming")
    assert respuesta_otro.status_code == 200, \
        "misma IP (mismo TestClient), otro usuario: no puede heredar el 429 del primero"
