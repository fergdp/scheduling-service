import logging
import base64
import os
import threading
from fastapi import Header, HTTPException, Depends, Cookie
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from jose import jwt, JWTError
from sqlalchemy import create_engine, text
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
# `clinic_engine` solo hace el SELECT del cross-check #82 H1 (cacheado TTL 60s)
# → pool default-ish alcanza, no necesita 20.
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

def get_current_user(
    token_cookie: Optional[str] = Cookie(None, alias="token"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)
) -> Optional[dict]:
    token = token_cookie or (credentials.credentials if credentials else None)
    
    if not token:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY_BYTES, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None

# Defense-in-depth (#82 H1): cache user_id -> clinic_id de DB con TTL 60s para
# detectar JWT con clinic_id manipulado sin pegarle a DB en cada request.
_MISSING = object()
_user_clinic_cache: TTLCache = TTLCache(maxsize=10000, ttl=60)
_user_clinic_cache_lock = threading.Lock()


def _resolve_db_clinic_id(user_id: int) -> Optional[int]:
    with _user_clinic_cache_lock:
        cached = _user_clinic_cache.get(user_id, _MISSING)
    if cached is not _MISSING:
        return cached

    # `users` vive en la DB de dental-clinic, no en la de scheduling.
    db = ClinicSessionLocal()
    try:
        row = db.execute(
            text("SELECT clinic_id FROM users WHERE user_id = :uid"),
            {"uid": user_id},
        ).first()
    finally:
        db.close()

    db_clinic_id = row[0] if row else None
    with _user_clinic_cache_lock:
        _user_clinic_cache[user_id] = db_clinic_id
    return db_clinic_id


def get_clinic_id(payload: dict = Depends(get_current_user)) -> int:
    if payload is None:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing token")

    clinic_id = payload.get("clinic_id")
    user_id = payload.get("user_id")
    if not clinic_id:
        raise HTTPException(status_code=401, detail="Clinic ID not found in token")
    if not user_id:
        logger.error("Token sin user_id — fail-close")
        raise HTTPException(status_code=401, detail="Invalid token")

    db_clinic_id = _resolve_db_clinic_id(int(user_id))
    if db_clinic_id is None:
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
    Factory de dependencia: lanza 403 si el usuario no tiene ninguno de los roles requeridos.

    Uso:
        @router.get("/url", dependencies=[require_any_role("DENTIST", "ADMIN")])
        async def my_endpoint(...):
    """
    def _check(roles: list[str] = Depends(get_roles)) -> list[str]:
        normalized = {r.upper() for r in allowed_roles}
        if not any(r in normalized for r in roles):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required: {' or '.join(allowed_roles)}"
            )
        return roles
    return Depends(_check)
