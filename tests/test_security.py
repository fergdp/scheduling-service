"""
Tests del middleware de CSRF (issue #262).

Antes de estos tests la única cobertura era un `assert response.status_code != 403`
que pasaba en verde **con el CSRF completamente apagado**: no distinguía "el token
se validó y estaba bien" de "no se validó nada". El coverage lo mostraba —
csrf_middleware.py llegaba al 74% con toda la rama de validación sin ejecutar.

Cada rechazo va acompañado de su **control positivo**: la misma request con el token
correcto tiene que llegar al handler. Sin esa mitad, un middleware que rechazara
TODO pasaría los tests de rechazo y rompería turnos entero.
"""
import pytest

from csrf_middleware import (
    CSRF_COOKIE_NAME,
    CSRF_EXEMPT_EXACT,
    CSRF_EXEMPT_PREFIX,
    CSRF_HEADER_NAME,
    CSRF_PROTECTED_METHODS,
    is_csrf_exempt,
)
from main import app

BASE = "/clinic-scheduling-api/v1"

# Las cuatro rutas que mutan estado, con el código que devuelve cada una cuando la
# request llega al handler. Son códigos DISTINTOS entre sí y ninguno es 403: eso es
# lo que vuelve al control positivo una prueba de que pasó de largo el middleware y
# no un "no dio 403" que también sería cierto con el CSRF apagado.
RUTAS_MUTANTES = [
    ("POST", f"{BASE}/appointments/", {}, 422),                     # falta el body
    ("PUT", f"{BASE}/appointments/999999", {}, 404),                # no existe
    ("PATCH", f"{BASE}/appointments/999999/status", {}, 422),       # falta el body
    ("DELETE", f"{BASE}/oauth/disconnect", None, 200),              # no-op, borra nada
]

ID_INEXISTENTE = 999999


@pytest.fixture
def cliente(client_sin_reraise):
    """
    Alias local de la fixture de conftest, que devuelve las respuestas de error en vez
    de re-lanzarlas — o sea, lo que ve un browser. El porqué está en su docstring.

    Es una fixture de conftest y no una construida acá a propósito: importar
    `tests.conftest` desde un test lo carga por SEGUNDA vez, con su propio engine, y
    el `setup_database` autouse crea las tablas en el otro. Se manifiesta como
    "no such table: appointments".
    """
    return client_sin_reraise


def _token_fresco(cliente) -> str:
    """
    Pide la cookie XSRF a una ruta exenta y la devuelve.

    Va contra /health/live y no contra "/" porque la raíz tiene rate limit de 5/minuto
    y varios tests piden token más de una vez.
    """
    cliente.get("/health/live")
    token = cliente.cookies.get(CSRF_COOKIE_NAME)
    assert token, "una ruta exenta tiene que sembrar la cookie XSRF-TOKEN"
    return token


# =============================================================================
# El bug del #262: la validación no corría
# =============================================================================

@pytest.mark.parametrize("metodo,ruta,body,_", RUTAS_MUTANTES)
def test_sin_cookie_ni_header_es_403(cliente, metodo, ruta, body, _):
    """
    Este es EL test del issue. Antes del arreglo esta misma request devolvía 422
    (POST) o 404 (PUT): entraba al handler sin CSRF ninguno, porque la lista de
    exentas se evaluaba con startswith contra una lista que incluía "/", y "/" es
    prefijo de toda ruta.
    """
    cliente.cookies.clear()
    res = cliente.request(metodo, ruta, json=body)

    assert res.status_code == 403, (
        f"{metodo} {ruta} sin token CSRF tiene que ser 403. "
        f"Un 422/404/200 significa que llegó al handler (el bug del #262); "
        f"un 500 significa que el rechazo volvió a ser un `raise` (ver el módulo)."
    )
    assert res.json() == {"detail": "CSRF token validation failed"}


@pytest.mark.parametrize("metodo,ruta,body,esperado", RUTAS_MUTANTES)
def test_con_el_token_correcto_llega_al_handler(cliente, metodo, ruta, body, esperado):
    """
    Control positivo, y la mitad más importante del set: un middleware que rechazara
    todo pasaría los tests de arriba y dejaría turnos inutilizable.

    Se afirma el código EXACTO de cada endpoint —422, 404, 422, 200— y no un
    `!= 403`, que es lo que había antes y era cierto también con el CSRF apagado.
    """
    token = _token_fresco(cliente)
    res = cliente.request(metodo, ruta, json=body, headers={CSRF_HEADER_NAME: token})

    assert res.status_code == esperado, (
        f"{metodo} {ruta} con cookie y header iguales tiene que atravesar el "
        f"middleware y que conteste el handler ({esperado})"
    )


def test_cookie_sin_header_es_403(cliente):
    """El caso frecuente de verdad: la cookie está pero el cliente no manda el header."""
    _token_fresco(cliente)
    res = cliente.post(f"{BASE}/appointments/", json={})
    assert res.status_code == 403


def test_header_sin_cookie_es_403(cliente):
    """Un atacante puede inventar el header; lo que no puede es leer la cookie."""
    cliente.cookies.clear()
    res = cliente.post(
        f"{BASE}/appointments/", json={}, headers={CSRF_HEADER_NAME: "inventado"}
    )
    assert res.status_code == 403


def test_tokens_distintos_es_403(cliente):
    """El corazón del double-submit: no alcanza con que existan los dos, tienen que ser iguales."""
    token = _token_fresco(cliente)
    res = cliente.post(
        f"{BASE}/appointments/", json={}, headers={CSRF_HEADER_NAME: token + "x"}
    )
    assert res.status_code == 403


def test_el_rechazo_no_es_un_500(cliente):
    """
    El segundo bug del #262, separado del primero a propósito.

    Starlette **no** pasa por los exception handlers de FastAPI lo que se levanta
    dentro de un BaseHTTPMiddleware: el ExceptionMiddleware que traduce
    HTTPException vive más adentro que los middlewares de usuario. Un
    `raise HTTPException(403)` acá sube hasta ServerErrorMiddleware y sale 500.
    Medido: arreglando sólo la lista de exentas, este POST daba
    500 {"message":"Internal Server Error"}.
    """
    cliente.cookies.clear()
    res = cliente.post(f"{BASE}/appointments/", json={})

    assert res.status_code != 500, (
        "el rechazo tiene que ser un JSONResponse devuelto, no un `raise`"
    )
    assert res.status_code == 403
    assert "detail" in res.json(), "un 500 traería {'message': ...}, no {'detail': ...}"


def test_los_get_no_se_bloquean(cliente):
    """Sólo se validan los métodos que mutan. Sin esto, la agenda no cargaría."""
    cliente.cookies.clear()
    res = cliente.get(f"{BASE}/appointments/")
    assert res.status_code == 200


# =============================================================================
# Las rutas exentas siguen andando
# =============================================================================

@pytest.mark.parametrize(
    "ruta", ["/", "/docs", "/redoc", "/openapi.json", "/health", "/health/live", "/health/ready"]
)
def test_las_rutas_exentas_responden_y_siembran_la_cookie(cliente, ruta):
    """
    Control positivo del otro lado: el arreglo no puede haber dejado afuera una ruta
    que sí tenía que estar exenta. Y son las que le dan al SPA su primera cookie XSRF:
    si dejaran de sembrarla, el front no podría mandar el header y quedaría todo en 403.
    """
    cliente.cookies.clear()
    res = cliente.get(ruta)

    assert res.status_code == 200
    assert CSRF_COOKIE_NAME in res.headers.get("set-cookie", "")


# =============================================================================
# Guard estructural: que no vuelva a apagarse el middleware entero
# =============================================================================

def _rutas_de_la_app():
    """Rutas registradas con al menos un método que muta, sin tocar API privada de FastAPI."""
    encontradas = []
    for ruta in app.routes:
        path = getattr(ruta, "path", None)
        metodos = getattr(ruta, "methods", None) or set()
        if path and (metodos & set(CSRF_PROTECTED_METHODS)):
            encontradas.append((path, sorted(metodos & set(CSRF_PROTECTED_METHODS))))
    return encontradas


def test_ninguna_ruta_que_muta_quedo_exenta():
    """
    El guard contra la clase de bug del #262, no contra su instancia.

    El bug no fue "alguien escribió mal una ruta": fue que la lista de exentas tragó
    la aplicación entera y nada lo dijo. Este test recorre las rutas REALES que
    registra la app y verifica que ninguna de las que mutan estado caiga en la
    exención — así que agregar mañana un prefijo demasiado ancho lo pone en rojo,
    aunque el resto de la suite siga verde.
    """
    rutas = _rutas_de_la_app()

    # Sanity: si el walk deja de encontrar rutas (cambia el router, cambia FastAPI),
    # el bucle de abajo queda vacío y este test pasaría sin mirar nada.
    assert len(rutas) >= 4, (
        f"se esperaban al menos las 4 rutas mutantes conocidas y se encontraron "
        f"{len(rutas)}: el recorrido de app.routes dejó de funcionar"
    )

    exentas = [(p, m) for p, m in rutas if is_csrf_exempt(p)]
    assert exentas == [], f"estas rutas mutan estado y están exentas de CSRF: {exentas}"


def test_ningun_prefijo_exento_tapa_toda_la_app():
    """
    La causa exacta del #262, fijada como invariante: "/" no puede estar entre los
    prefijos. Como prefijo matchea todo; como match exacto (que es donde está ahora)
    sólo matchea la raíz.
    """
    assert "/" not in CSRF_EXEMPT_PREFIX
    assert "/" in CSRF_EXEMPT_EXACT, "la raíz sí tiene que estar exenta, pero por igualdad"

    for prefijo in CSRF_EXEMPT_PREFIX:
        assert len(prefijo) > 1 and prefijo.startswith("/"), (
            f"{prefijo!r} no es un prefijo utilizable"
        )
        assert not is_csrf_exempt("/una/ruta/cualquiera/inventada"), (
            f"el prefijo {prefijo!r} está eximiendo rutas que no le corresponden"
        )


@pytest.mark.parametrize(
    "path,exento",
    [
        ("/", True),
        ("/docs", True),
        ("/health", True),
        ("/health/live", True),
        ("/health/ready", True),
        # El corte del prefijo es en la barra: sin eso, cualquier ruta que EMPIECE
        # con las letras de una exenta se colaría.
        ("/healthz-falso", False),
        ("/health-insurances", False),
        ("/docs-internos", False),
        (f"{BASE}/appointments/", False),
        (f"{BASE}/oauth/disconnect", False),
        ("/metrics", False),
    ],
)
def test_el_predicado_de_exencion(path, exento):
    """Unit test del predicado solo, sin levantar la app: el bug vivía acá adentro."""
    assert is_csrf_exempt(path) is exento
