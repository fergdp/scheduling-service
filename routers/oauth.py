import logging
import os
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Cookie
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import Optional
from jose import jwt, JWTError
from dependencies import get_db, get_clinic_id, get_user_id, require_any_role
from utils.google_calendar import get_google_auth_url, exchange_code_for_tokens, get_google_user_email
from utils.crypto import encrypt_token
from models import DentistCalendarConfig
from schemas import OAuthUrlResponse
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID  = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
_IS_PRODUCTION    = os.getenv("APP_ENVIRONMENT", "development").lower() == "production"
_OAUTH_STATE_COOKIE = "oauth_state"
_ALGORITHM        = "HS256"
_STATE_TTL_MINUTES = 10

SECRET_KEY_RAW = os.getenv("JWT_SECRET_KEY", "")
try:
    import base64
    _SECRET_BYTES = base64.b64decode(SECRET_KEY_RAW)
except Exception:
    _SECRET_BYTES = SECRET_KEY_RAW.encode()

router = APIRouter()


def _make_state_jwt(user_id: int, clinic_id: int) -> str:
    """Genera un JWT corto con identidad del dentista para usar como state de OAuth."""
    exp = datetime.now(timezone.utc) + timedelta(minutes=_STATE_TTL_MINUTES)
    return jwt.encode(
        {"user_id": user_id, "clinic_id": clinic_id, "exp": exp},
        _SECRET_BYTES,
        algorithm=_ALGORITHM,
    )


def _decode_state_jwt(state: str) -> dict:
    """Valida firma y expiración del state JWT. Lanza HTTPException si es inválido."""
    try:
        return jwt.decode(state, _SECRET_BYTES, algorithms=[_ALGORITHM])
    except JWTError as e:
        logger.warning(f"Invalid OAuth state JWT: {e}")
        raise HTTPException(status_code=400, detail="Invalid OAuth state — possible CSRF attack")


@router.get("/status", dependencies=[require_any_role("DENTIST", "ADMIN")])
async def get_gcal_status(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_user_id),
    clinic_id: int = Depends(get_clinic_id)
):
    config = db.query(DentistCalendarConfig).filter(
        DentistCalendarConfig.dentist_user_id == user_id,
        DentistCalendarConfig.clinic_id == clinic_id
    ).first()
    connected = bool(config and config.google_refresh_token)
    return {
        "connected": connected,
        "email": config.google_email if connected else None,
        "sync_enabled": config.sync_enabled if connected else False,
    }


@router.delete("/disconnect", dependencies=[require_any_role("DENTIST", "ADMIN")])
async def disconnect_gcal(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_user_id),
    clinic_id: int = Depends(get_clinic_id)
):
    config = db.query(DentistCalendarConfig).filter(
        DentistCalendarConfig.dentist_user_id == user_id,
        DentistCalendarConfig.clinic_id == clinic_id
    ).first()
    if config:
        config.google_access_token  = None
        config.google_refresh_token = None
        config.token_expiry         = None
        config.google_email         = None
        config.sync_enabled         = False
        db.commit()
    logger.info(f"Google Calendar disconnected for dentist {user_id} (clinic {clinic_id})")
    return {"status": "disconnected"}


@router.get("/url", response_model=OAuthUrlResponse, dependencies=[require_any_role("DENTIST", "ADMIN")])
async def get_auth_url(
    response: Response,
    user_id: int = Depends(get_user_id),
    clinic_id: int = Depends(get_clinic_id)
):
    """
    Genera la URL de Google OAuth. El state es un JWT corto firmado con
    user_id + clinic_id para que el callback (público) pueda identificar al dentista
    sin depender de la cookie de sesión, que no llega en redirects top-level.
    """
    logger.info(f"Generating Google OAuth URL for dentist_id: {user_id} (clinic_id: {clinic_id})")
    try:
        state = _make_state_jwt(user_id, clinic_id)
        auth_url = get_google_auth_url(state)

        # Guardamos el mismo JWT en cookie httpOnly (samesite=lax sobrevive el redirect top-level).
        # En /callback comparamos cookie vs query-param como defensa en profundidad.
        response.set_cookie(
            key=_OAUTH_STATE_COOKIE,
            value=state,
            max_age=_STATE_TTL_MINUTES * 60,
            httponly=True,
            secure=_IS_PRODUCTION,
            samesite="lax",
            path="/"
        )

        logger.info(f"OAuth URL successfully generated for dentist_id: {user_id}")
        return OAuthUrlResponse(auth_url=auth_url)
    except Exception as e:
        logger.error(f"Error generating OAuth URL for dentist {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate authorization URL")


# Callback es PÚBLICO — el redirect de Google es top-level y no envía la cookie
# de sesión httpOnly. La identidad del dentista viene del state JWT firmado.
@router.get("/callback")
async def oauth_callback(
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    stored_state: Optional[str] = Cookie(None, alias=_OAUTH_STATE_COOKIE),
    db: Session = Depends(get_db),
):
    """
    Callback de Google. Valida el state JWT (firma + expiración) y lo compara
    con la cookie oauth_state como segunda línea de defensa anti-CSRF.
    """
    # 1. Validar firma y expiración del state JWT → extrae identidad
    payload = _decode_state_jwt(state)
    user_id  = payload["user_id"]
    clinic_id = payload["clinic_id"]

    # 2. Defensa en profundidad: comparar query-param vs cookie (double-submit)
    if stored_state and stored_state != state:
        logger.warning(f"OAuth state cookie mismatch for user {user_id}")
        raise HTTPException(status_code=400, detail="Invalid OAuth state — possible CSRF attack")

    response.delete_cookie(key=_OAUTH_STATE_COOKIE, path="/")
    logger.info(f"Received Google OAuth callback for user_id: {user_id} (clinic_id: {clinic_id})")

    try:
        tokens = exchange_code_for_tokens(code)

        creds = Credentials(
            token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET
        )
        google_email = get_google_user_email(creds)
        logger.info(f"OAuth tokens obtained for Google account: {google_email} (dentist_id: {user_id})")

        encrypted_access  = encrypt_token(tokens["access_token"])
        encrypted_refresh = encrypt_token(tokens["refresh_token"])
        token_expiry      = tokens["token_expiry"]

        config = db.query(DentistCalendarConfig).filter(
            DentistCalendarConfig.dentist_user_id == user_id,
            DentistCalendarConfig.clinic_id == clinic_id
        ).first()

        if not config:
            config = DentistCalendarConfig(dentist_user_id=user_id, clinic_id=clinic_id)
            db.add(config)

        config.google_access_token  = encrypted_access
        config.google_refresh_token = encrypted_refresh
        config.token_expiry         = token_expiry
        config.google_email         = google_email
        config.sync_enabled         = True
        db.commit()

        logger.info(f"Successfully linked Google account {google_email} for dentist {user_id} in clinic {clinic_id}")

        return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><title>Google Calendar Vinculado</title>
<style>body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}}
.box{{background:#fff;padding:2rem 3rem;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.12);text-align:center}}
h2{{color:#1976d2;margin-bottom:.5rem}}p{{color:#555;margin-bottom:1.5rem}}
.email{{font-weight:bold;color:#333}}</style></head>
<body><div class="box">
<h2>✓ Google Calendar Vinculado</h2>
<p>Tu cuenta <span class="email">{google_email}</span><br>fue conectada con éxito.</p>
<p>Podés cerrar esta ventana.</p>
</div>
<script>
  if(window.opener){{window.opener.postMessage({{type:'gcal_connected',email:'{google_email}'}},'*');}}
  setTimeout(function(){{window.close();}},2000);
</script></body></html>""")
    except Exception as e:
        logger.error(f"Error in OAuth callback for dentist {user_id}: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=400, detail="Error vinculando cuenta de Google")
