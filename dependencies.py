import logging
import base64
import os
from fastapi import Header, HTTPException, Depends, Cookie
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from jose import jwt, JWTError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, with_loader_criteria
from dotenv import load_dotenv
from contextvars import ContextVar

load_dotenv()

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

# SQL Guard Context
current_clinic_id: ContextVar[Optional[int]] = ContextVar("current_clinic_id", default=None)

engine = create_engine(DATABASE_URL, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    clinic_id = current_clinic_id.get()
    
    if clinic_id:
        # SQL Guard: Inyectar automáticamente el filtro clinic_id en TODAS las consultas
        db.execute(
            with_loader_criteria(
                lambda cls: getattr(cls, "clinic_id", None) == clinic_id,
                include_subclasses=True
            )
        )
        
    try:
        yield db
    except Exception as e:
        logger.exception("Database connection error")
        raise HTTPException(status_code=500, detail="Database connection error")
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

def get_clinic_id(payload: dict = Depends(get_current_user)) -> int:
    if payload is None:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing token")

    clinic_id = payload.get("clinic_id")
    if not clinic_id:
        raise HTTPException(status_code=401, detail="Clinic ID not found in token")

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
