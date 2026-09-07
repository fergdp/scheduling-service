"""
CSRF double-submit-cookie middleware.

Protege POST/PUT/PATCH/DELETE comparando el header X-XSRF-TOKEN con la cookie
XSRF-TOKEN. Las rutas exentas (raíz, docs, health) reciben/refrescan la cookie
pero no validan el header.

Los dos bugs del issue #262
---------------------------
Este archivo estaba desactivado de hecho, por dos errores que se tapaban entre sí:

1. La lista de exentas se evaluaba con ``any(path.startswith(r) for r in [..., "/", ...])``
   y **"/" es prefijo de todo**, así que TODA ruta quedaba exenta. Medido antes de
   tocar nada: un POST a /clinic-scheduling-api/v1/appointments/ sin cookie ni header
   devolvía **422** — o sea que llegaba a validar el body. El coverage lo decía
   también: las líneas de la validación nunca se ejecutaban.

2. El rechazo era ``raise HTTPException(403)``. Starlette **no** pasa las excepciones
   levantadas dentro de un ``BaseHTTPMiddleware`` por los exception handlers de
   FastAPI: el ``ExceptionMiddleware`` que las traduce vive *más adentro* que los
   middlewares de usuario. Sube hasta ``ServerErrorMiddleware`` y sale **500**.
   Medido: arreglando sólo el punto 1, ese mismo POST pasaba de 422 a
   ``500 {"message":"Internal Server Error"}``.

   Por eso el rechazo es un ``JSONResponse`` que se **devuelve**, nunca un ``raise``.
   Mismo bug y mismo arreglo que en ai-orchestration-service y quoting-service; en
   health-insurance-service sigue abierto (#263).

⚠️ Al testear esto, el ``TestClient`` por defecto trae ``raise_server_exceptions=True``
y **re-lanza** la excepción del servidor en el proceso del test. Con el ``raise``
puesto, un test así ve un ``HTTPException 403`` y pasa en verde mientras el cliente
real recibe un 500. El instrumento miente justo en la dirección que oculta el bug:
los tests de este archivo usan ``raise_server_exceptions=False``.
"""
import logging
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

APP_ENVIRONMENT = os.getenv("APP_ENVIRONMENT", "development").lower()
IS_PRODUCTION = APP_ENVIRONMENT == "production"

CSRF_TOKEN_LENGTH = 32
CSRF_COOKIE_NAME = "XSRF-TOKEN"
CSRF_HEADER_NAME = "X-XSRF-TOKEN"

CSRF_COOKIE_CONFIG = {
    "httponly": False,  # el SPA tiene que poder leerla (axios withXSRFToken)
    "secure": IS_PRODUCTION,
    "samesite": "strict" if IS_PRODUCTION else "lax",
    "max_age": 3600,
    "path": "/",
}

# Match EXACTO. Va en un set y se compara con `==`, no con startswith: "/" es
# prefijo de toda ruta y ponerlo en una lista de prefijos apaga el CSRF entero
# (issue #262). Si agregás algo acá, tiene que ser una ruta completa.
CSRF_EXEMPT_EXACT = frozenset({"/", "/docs", "/redoc", "/openapi.json"})

# Match por prefijo, sólo para rutas con sub-rutas (/health, /health/live,
# /health/ready). El corte es en el separador — `path == p or path.startswith(p + "/")`—
# para que "/healthz-loquesea" NO quede exento por parecerse.
CSRF_EXEMPT_PREFIX = ("/health",)

# /metrics NO está exento a propósito: es GET, y los GET no se validan nunca, así
# que la exención no haría nada. Cada entrada de estas listas es superficie para
# el bug del #262 — la regla es no agregar ninguna que no sea imprescindible.

CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_LENGTH)


def is_csrf_exempt(path: str) -> bool:
    """True si `path` queda fuera de la validación de CSRF."""
    if path in CSRF_EXEMPT_EXACT:
        return True
    return any(path == p or path.startswith(p + "/") for p in CSRF_EXEMPT_PREFIX)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        if is_csrf_exempt(path):
            response = await call_next(request)
            response.set_cookie(
                key=CSRF_COOKIE_NAME, value=generate_csrf_token(), **CSRF_COOKIE_CONFIG
            )
            return response

        csrf_cookie_token = request.cookies.get(CSRF_COOKIE_NAME)

        if method in CSRF_PROTECTED_METHODS:
            csrf_header_token = request.headers.get(CSRF_HEADER_NAME)
            # Comparación con `!=` y no con `secrets.compare_digest`, igual que en
            # quoting y ai-orch. No es un descuido: compare_digest sobre `str` LANZA
            # TypeError si alguno tiene caracteres no-ASCII, y los headers los decodifica
            # Starlette en latin-1 — o sea que un header con bytes >127 pasaría de 403 a
            # 500. El ataque de timing tampoco aplica acá: el atacante no puede leer la
            # cookie (same-origin), así que no tiene contra qué medir; tendría que
            # adivinar 256 bits a ciegas.
            if (
                not csrf_cookie_token
                or not csrf_header_token
                or csrf_cookie_token != csrf_header_token
            ):
                # Se loguea CUÁL de las tres condiciones falló porque desde el
                # browser las tres se ven igual (un 403 pelado) y la causa más
                # común no es un ataque sino la cookie ausente.
                logging.warning(
                    "CSRF rechazado: %s %s (cookie=%s header=%s)",
                    method,
                    path,
                    "si" if csrf_cookie_token else "NO",
                    "si" if csrf_header_token else "NO",
                )
                # `return`, no `raise` — ver el docstring del módulo.
                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token validation failed"},
                )

        response = await call_next(request)
        # Se preserva el token que ya traía: regenerarlo en cada respuesta rompe
        # al front cuando manda dos mutaciones seguidas con el valor cacheado.
        csrf_token = csrf_cookie_token if csrf_cookie_token else generate_csrf_token()
        response.set_cookie(key=CSRF_COOKIE_NAME, value=csrf_token, **CSRF_COOKIE_CONFIG)
        return response
