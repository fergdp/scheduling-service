import logging
import secrets
import os
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Cookie
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import Optional
from dependencies import get_db, get_clinic_id, get_user_id, require_any_role
from utils.google_calendar import get_google_auth_url, exchange_code_for_tokens, get_google_user_email
from utils.crypto import encrypt_token
from models import DentistCalendarConfig
from schemas import OAuthUrlResponse
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
_IS_PRODUCTION = os.getenv("APP_ENVIRONMENT", "development").lower() == "production"
_OAUTH_STATE_COOKIE = "oauth_state"

router = APIRouter()


@router.get("/status", dependencies=[require_any_role("DENTIST", "ADMIN")])
async def get_gcal_status(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_user_id),
    clinic_id: int = Depends(get_clinic_id)
):
    """Devuelve si el odontólogo tiene Google Calendar conectado."""
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
    """Desvincula la cuenta de Google Calendar del odontólogo."""
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
    """Genera la URL de Google OAuth para que el odontólogo vincule su cuenta."""
    logger.info(f"Generating Google OAuth URL for dentist_id: {user_id} (clinic_id: {clinic_id})")
    try:
        state = secrets.token_urlsafe(32)
        auth_url = get_google_auth_url(state)

        # State almacenado en cookie httpOnly de corta vida para validar en el callback (anti-CSRF).
        response.set_cookie(
            key=_OAUTH_STATE_COOKIE,
            value=state,
            max_age=600,
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


@router.get("/callback", dependencies=[require_any_role("DENTIST", "ADMIN")])
async def oauth_callback(
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    stored_state: Optional[str] = Cookie(None, alias=_OAUTH_STATE_COOKIE),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_user_id),
    clinic_id: int = Depends(get_clinic_id)
):
    """
    Callback de Google que recibe el código, lo intercambia por tokens
    y los vincula permanentemente al odontólogo en la base de datos.
    """
    # Validar state para prevenir CSRF en el flujo OAuth2
    if not stored_state or not secrets.compare_digest(state, stored_state):
        logger.warning(
            f"OAuth state mismatch for user {user_id}: "
            f"expected={stored_state!r} got={state!r}"
        )
        raise HTTPException(status_code=400, detail="Invalid OAuth state — possible CSRF attack")

    # Invalidar la cookie de state (token de un solo uso)
    response.delete_cookie(key=_OAUTH_STATE_COOKIE, path="/")

    logger.info(f"Received Google OAuth callback for user_id: {user_id} (clinic_id: {clinic_id})")
    try:
        # 1. Intercambiar código por tokens
        tokens = exchange_code_for_tokens(code)

        # 2. Obtener el email de Google para registro visual
        creds = Credentials(
            token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET
        )
        google_email = get_google_user_email(creds)
        logger.info(f"OAuth tokens obtained for Google account: {google_email} (dentist_id: {user_id})")

        # 3. Encriptar los tokens para seguridad "at-rest"
        encrypted_access = encrypt_token(tokens["access_token"])
        encrypted_refresh = encrypt_token(tokens["refresh_token"])
        token_expiry = tokens["token_expiry"]

        # 4. Vincular con el odontólogo en la base de datos
        config = db.query(DentistCalendarConfig).filter(
            DentistCalendarConfig.dentist_user_id == user_id,
            DentistCalendarConfig.clinic_id == clinic_id
        ).first()

        if not config:
            config = DentistCalendarConfig(
                dentist_user_id=user_id,
                clinic_id=clinic_id
            )
            db.add(config)

        config.google_access_token = encrypted_access
        config.google_refresh_token = encrypted_refresh
        config.token_expiry = token_expiry
        config.google_email = google_email
        config.sync_enabled = True

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
