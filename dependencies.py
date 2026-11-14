import logging
import base64
import os
import threading
from collections import namedtuple
from fastapi import Header, HTTPException, Depends, Cookie, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
import jwt
from jwt import InvalidTokenError
from slowapi.util import get_remote_address
from sqlalchemy import create_engine, text, bindparam
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker, Session
from cachetools import TTLCache
from contextvars import ContextVar

# Logger configuration
logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)

SECRET_KEY_RAW = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"

if not SECRET_KEY_RAW:
    raise ValueError("JWT_SECRET_KEY environment variable not set.")

try:
    SECRET_KEY_BYTES = base64.b64decode(SECRET_KEY_RAW)
except Exception as e:
    logger.error(f"Error decoding secret key: {e}")
    raise ValueError("Invalid JWT_SECRET_KEY format.")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set.")

# DB principal (dental-clinic) — solo para resolver user_id → clinic_id en el
# cross-check de #82 H1. La tabla `users` vive ahí, no en la DB de scheduling.
CLINIC_DATABASE_URL = os.getenv("CLINIC_DATABASE_URL")
if not CLINIC_DATABASE_URL:
    raise ValueError("CLINIC_DATABASE_URL environment variable not set.")

# SQL Guard Context
current_clinic_id: ContextVar[Optional[int]] = ContextVar("current_clinic_id", default=None)

# Pool sizing — issue #33.
# `engine` recibe todo el tráfico de appointments → pool grande.
#
# `clinic_engine` (#315): YA NO es sólo el SELECT cacheado del cross-check #82 H1 (TTL 60s).
# Desde el #313 y el #314, `telefonos_vigentes_de` usa el MISMO engine sin caché a propósito
# (acá la frescura importa más que el costo de un SELECT de más) desde 10 endpoints de lectura
# entre `routers/appointments.py` y `routers/waitlist.py`. `pool_size=5, max_overflow=10` (15
# conexiones) sigue siendo razonable para la escala actual —consultas puntuales por `user_id`,
# clínicas chicas—, pero es una estimación, no una medición: no hay forma de probar el
# agotamiento real del pool contra SQLite (`_make_engine` le ignora `pool_size`/`max_overflow`
# a propósito, ver abajo). Si el tráfico concurrente crece, medir con un load test contra
# MySQL/MariaDB antes de tocar el número.
def _make_engine(url: str, pool_size: int, max_overflow: int):
    # SQLite usa SingletonThreadPool y no acepta pool_size/max_overflow/pool_timeout.
    # En tests (sqlite://) caemos a defaults; en prod (mysql+pymysql) aplican los pool kwargs.
    if url.startswith("sqlite"):
        return create_engine(url, pool_recycle=3600)
    return create_engine(
        url,
        pool_recycle=3600,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=30,
    )


engine = _make_engine(DATABASE_URL, pool_size=20, max_overflow=40)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

clinic_engine = _make_engine(CLINIC_DATABASE_URL, pool_size=5, max_overflow=10)
ClinicSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=clinic_engine)

def get_db():
    # Multi-tenancy: NO hay SQL Guard automático.
    # Cada endpoint es responsable de filtrar explícitamente por clinic_id
    # (e.g. Appointment.clinic_id == clinic_id).
    # El valor de current_clinic_id se usa como referencia en los endpoints
    # pero no aplica filtros a nivel de sesión.
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # Rollback explícito para evitar que la transacción quede abierta y
        # bloquee filas hasta que el pool resetee la conexión. Re-raise la
        # excepción original — no la envolvemos en HTTP 500 para no tragar
        # HTTPException(404/403/422/etc.) que los endpoints lanzan legítimamente.
        db.rollback()
        raise
    finally:
        db.close()

def _payload_de(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET_KEY_BYTES, algorithms=[ALGORITHM])
    except InvalidTokenError:
        return None


def get_current_user(
    token_cookie: Optional[str] = Cookie(None, alias="token"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[dict]:
    """
    El payload del JWT, o `None` si no hay sesion: sin token, token invalido o vencido, o una
    cuenta que ya no puede entrar (#547). Todos los guards de este servicio pasan por aca y
    convierten el `None` en 401, incluidos los endpoints que solo piden `get_user_id`. Si la
    base de dental-clinic no responde, 503.
    """
    token = token_cookie or (credentials.credentials if credentials else None)
    payload = _payload_de(token)
    if payload is None or not _la_sesion_sigue_valida(payload):
        return None
    return payload


def key_por_usuario_o_ip(request: Request) -> str:
    """
    Clave del rate limiter (issue #303): por usuario, no por IP.

    `GET /v1/appointments/` estaba limitado por `get_remote_address`, y **toda una clínica sale
    a internet por el mismo router**: el tope de 60/min era, en los hechos, un tope por clínica
    entera. Una recepcionista y varios odontólogos mirando la agenda a la vez lo pasaban, y el
    backend respondía 429 — la agenda se veía vacía con «Error al cargar los turnos».

    Un `Request` crudo no pasa por las dependencias de FastAPI (`get_current_user` recibe la
    cookie y el header ya resueltos por la inyección de dependencias; el limiter llama a esto
    ANTES de eso), así que el token se lee a mano, con el mismo criterio: la cookie `token`
    primero, el header `Authorization: Bearer` después.

    Sin sesión válida —rutas públicas, o un pedido que igual llega sin JWT— cae a la IP: ahí sí
    hace falta un límite por origen.
    """
    token = request.cookies.get("token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:]
    payload = _payload_de(token)
    user_id = payload.get("user_id") if payload else None
    return f"user:{user_id}" if user_id is not None else get_remote_address(request)


# --- La fila de `users` que decide la sesion (#547) y la clinica (#82 H1) ------
# Una sola consulta por usuario, cacheada, contesta dos preguntas: si la cuenta puede seguir
# adentro y cual es su clinica. El #330 (la huella de la contrasena) va a sumar su columna a
# esta misma fila y su comparacion a `_la_sesion_sigue_valida`.
#
# En el cache hay una `FilaDeUsuario` o `_NO_EXISTE` (el usuario no esta en la base: se
# cachea igual). `_MISSING` es otra cosa: marca "no esta en el cache".
#
# TTL 60 s (#547). Es lo que tarda, como mucho, en cortarse la sesion
# de una cuenta deshabilitada o dada de baja, y en volver la de una reactivada. dental-clinic
# no cachea (#130), pero es el que emite el token y el front saca al usuario con el primer 401
# de dental-clinic: esta ventana solo la aprovecha quien le pegue directo a la API con la
# cookie. Mas corto cuesta una consulta por pedido, mas largo es la ventana del #547.
#
# ⚠️ Las columnas de este SELECT tienen que estar en el GRANT por columna del usuario de MySQL
# de este servicio (`COLUMNAS_USERS` en vps-hostinger/usuarios-mysql-384/usuarios_384.py). Si
# falta una, la consulta falla y NADIE entra: el GRANT va en el VPS antes que el deploy.
_MISSING = object()
_NO_EXISTE = object()
FilaDeUsuario = namedtuple("FilaDeUsuario", ["clinic_id", "is_enabled", "deactivated_at"])
_user_clinic_cache: TTLCache = TTLCache(maxsize=10000, ttl=60)
_user_clinic_cache_lock = threading.Lock()


def _fila_de_usuario(user_id: int):
    """La fila del usuario en la base (`FilaDeUsuario`) o `_NO_EXISTE`."""
    with _user_clinic_cache_lock:
        cached = _user_clinic_cache.get(user_id, _MISSING)
    if cached is not _MISSING:
        return cached

    # `users` vive en la DB de dental-clinic, no en la de scheduling.
    db = ClinicSessionLocal()
    try:
        row = db.execute(
            text("SELECT clinic_id, is_enabled, deactivated_at FROM users WHERE user_id = :uid"),
            {"uid": user_id},
        ).first()
    except SQLAlchemyError:
        # Falla cerrada, pero que se entienda: sin esto es un 500 genérico en TODOS los
        # endpoints con sesion. Tambien es lo que se ve si falta el GRANT de una columna.
        logger.error(
            "No se pudo leer users de dental-clinic para validar la sesion (#547): "
            "¿base caida o falta el GRANT de una columna?",
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="Servicio no disponible")
    finally:
        db.close()

    fila = _NO_EXISTE if row is None else FilaDeUsuario(row[0], row[1], row[2])
    with _user_clinic_cache_lock:
        _user_clinic_cache[user_id] = fila
    return fila


def _resolve_db_clinic_id(user_id: int) -> Optional[int]:
    """La clinica del usuario segun la base, o `None` si no tiene o si no existe (la regla vieja, #284)."""
    fila = _fila_de_usuario(user_id)
    return None if fila is _NO_EXISTE else fila.clinic_id


def _bit_verdadero(valor) -> bool:
    """
    `users.is_enabled` es BIT(1) en MySQL y pymysql lo devuelve como bytes: `b'\\x00'` es
    VERDADERO en Python, asi que un `if fila.is_enabled` dejaria entrar a todos. Vacio
    (`NULL`) no entra.
    """
    if valor is None:
        return False
    if isinstance(valor, (bytes, bytearray)):
        return int.from_bytes(valor, "big") != 0
    return bool(valor)


def puede_entrar(fila: FilaDeUsuario) -> bool:
    """
    Si la cuenta puede seguir adentro: habilitada y sin la marca de baja. Es la regla de
    dental-clinic (`CustomUserDetails`) y de clinical-history y onboarding
    (`CustomUserDetailsService.puedeEntrar`, #415): si cambia, cambia en todos.

    Valen las dos columnas: un dado de baja puede quedar con `is_enabled = 1` (#485). Al
    paciente que espera aprobacion (`is_enabled = 0`) esto no le corta de mas, porque
    dental-clinic no le da token. ⚠️ En las listas, «dado de baja» es solo `deactivated_at`:
    no usar esta funcion para filtrar pacientes.
    """
    return _bit_verdadero(fila.is_enabled) and fila.deactivated_at is None


def _la_sesion_sigue_valida(payload: dict) -> bool:
    """
    Si el usuario del token todavia puede usar este servicio (#547). Falla cerrada: un token
    sin `user_id` o de un usuario que ya no esta en la base tampoco pasa.
    """
    user_id = payload.get("user_id")
    if not user_id:
        logger.error("Token sin user_id — sin sesion")
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        logger.error("Token con un user_id que no es un numero — sin sesion")
        return False
    fila = _fila_de_usuario(uid)
    if fila is _NO_EXISTE:
        logger.warning(f"user_id={user_id} no existe en la base — sin sesion")
        return False
    if not puede_entrar(fila):
        logger.warning(f"user_id={user_id} deshabilitado o dado de baja — sin sesion (#547)")
        return False
    # #330: aca se suma comparar la huella de la contrasena del token con la de `fila`.
    return True


def telefonos_vigentes_de(patient_user_ids) -> dict:
    """
    El teléfono ACTUAL de cada paciente, según `users` de dental-clinic (issue #313).

    El turno guarda `patient_phone` al crearse y nada lo actualiza después: si el paciente
    cambia de número y lo corrigen en su ficha, los turnos que ya tenía siguen con el viejo, y
    con eso el recordatorio por WhatsApp (#295) le llega a quien tenga hoy esa línea. Los
    endpoints de lectura llaman a esto para mostrar el vigente y sólo caer al guardado si el
    paciente no tiene uno cargado (mejor un número viejo que ninguno).

    Sin caché a propósito: es la misma tabla que `_resolve_db_clinic_id`, pero ahí un TTL de
    60 s es una demora aceptable para un cross-check de seguridad; acá sería mostrar el número
    viejo un minuto más, que es exactamente el bug que esto arregla.

    Un `IN` por página, no una consulta por turno: el propio issue medía ese costo antes de
    decidir este enfoque en vez de escribir el teléfono vigente en cada turno al corregirlo.
    """
    ids = {i for i in patient_user_ids if i}
    if not ids:
        return {}

    db = ClinicSessionLocal()
    try:
        filas = db.execute(
            text("SELECT user_id, phone FROM users WHERE user_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": list(ids)},
        ).all()
    finally:
        db.close()
    return {fila[0]: fila[1] for fila in filas if fila[1]}


def _is_admin(payload: dict) -> bool:
    """ROLE_ADMIN check con tolerancia al prefijo Spring (`ROLE_`)."""
    roles = payload.get("roles") or []
    return any(r.upper().removeprefix("ROLE_") == "ADMIN" for r in roles)


def get_clinic_id(payload: dict = Depends(get_current_user)) -> int:
    """
    Devuelve el clinic_id del JWT tras cross-check contra DB (issue #82 H1).

    Distincion 401 vs 403 — importante para el frontend:
      - 401: token invalido / ausente / mismatch malicioso → apiFactory redirige
        a /login con sessionExpired=true (sesion realmente termino).
      - 403: el user esta autenticado pero NO tiene contexto de clinica (admin
        global sin clinica). El recurso requiere clinic context y no aplica para
        este user; el frontend NO debe interpretar como sesion expirada — debe
        mostrar UI alternativa o esconder el feature. Ver DashboardPage que oculta
        DailyAgenda para admin sin clinica.
    """
    if payload is None:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing token")

    user_id = payload.get("user_id")
    if not user_id:
        logger.error("Token sin user_id — fail-close")
        raise HTTPException(status_code=401, detail="Invalid token")

    clinic_id = payload.get("clinic_id")
    is_admin = _is_admin(payload)

    # Admin global sin clinica en JWT: 403 explicito (no 401) — el resource necesita
    # clinic context que admin no tiene, pero el token es valido.
    if not clinic_id:
        if is_admin:
            raise HTTPException(
                status_code=403,
                detail="Admin sin clinica seleccionada. Este recurso requiere contexto de clinica."
            )
        raise HTTPException(status_code=401, detail="Clinic ID not found in token")

    db_clinic_id = _resolve_db_clinic_id(int(user_id))
    if db_clinic_id is None:
        # Admin con clinic_id en JWT pero NULL en DB → mismo razonamiento: 403.
        if is_admin:
            raise HTTPException(
                status_code=403,
                detail="Admin global sin clinica asignada en DB. Este recurso requiere contexto de clinica."
            )
        logger.error(f"SECURITY: user_id={user_id} no existe en DB — fail-close")
        raise HTTPException(status_code=401, detail="Invalid token")
    if int(db_clinic_id) != int(clinic_id):
        logger.error(
            f"SECURITY: clinic_id mismatch para user_id={user_id} — JWT={clinic_id} DB={db_clinic_id} — fail-close"
        )
        raise HTTPException(status_code=401, detail="Invalid token")

    clinic_id_int = int(clinic_id)
    current_clinic_id.set(clinic_id_int) # Activar el SQL Guard para esta petición
    return clinic_id_int

def get_user_id(payload: dict = Depends(get_current_user)) -> int:
    if payload is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    return int(user_id)

def get_roles(payload: Optional[dict] = Depends(get_current_user)) -> list[str]:
    """Extrae los roles del JWT, quita el prefijo ROLE_ y normaliza a mayúsculas.
    Spring Boot emite 'ROLE_ADMIN', 'ROLE_DENTIST', etc.
    Este servicio trabaja con 'ADMIN', 'DENTIST', etc.
    """
    if payload is None:
        return []
    return [r.upper().removeprefix("ROLE_") for r in payload.get("roles", [])]

def require_any_role(*allowed_roles: str):
    """
    Factory de dependencia: 401 si no hay sesion (#547) y 403 si el usuario no tiene ninguno de
    los roles requeridos.

    Uso:
        @router.get("/url", dependencies=[require_any_role("DENTIST", "ADMIN")])
        async def my_endpoint(...):
    """
    def _check(
        payload: Optional[dict] = Depends(get_current_user),
        roles: list[str] = Depends(get_roles),
    ) -> list[str]:
        # 401 y no 403 sin sesion (token vencido o cuenta cortada, #547): las rutas que ponen
        # este guard en `dependencies=[...]` lo corren ANTES que `get_clinic_id`, y solo el
        # 401 manda al front al login. Mismo `_check` que quoting.
        if payload is None:
            raise HTTPException(
                status_code=401, detail="Unauthorized: Invalid or missing token"
            )
        normalized = {r.upper() for r in allowed_roles}
        if not any(r in normalized for r in roles):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required: {' or '.join(allowed_roles)}"
            )
        return roles
    return Depends(_check)
