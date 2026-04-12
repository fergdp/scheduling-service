import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
import os
from dependencies import get_db, get_clinic_id, get_user_id
from utils.google_calendar import get_google_auth_url, exchange_code_for_tokens, get_google_user_email
from utils.crypto import encrypt_token
from models import DentistCalendarConfig
from schemas import OAuthUrlResponse
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

# Configuración de Google OAuth2 (vía .env)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

router = APIRouter()

@router.get("/url", response_model=OAuthUrlResponse)
async def get_auth_url(
    user_id: int = Depends(get_user_id),
    clinic_id: int = Depends(get_clinic_id)
):
    """Genera la URL de Google OAuth para que el odontólogo vincule su cuenta."""
    logger.info(f"Generating Google OAuth URL for dentist_id: {user_id} (clinic_id: {clinic_id})")
    try:
        auth_url = get_google_auth_url()
        logger.info(f"OAuth URL successfully generated for dentist_id: {user_id}")
        return OAuthUrlResponse(auth_url=auth_url)
    except Exception as e:
        logger.error(f"Error generating OAuth URL for dentist {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate authorization URL")

@router.get("/callback")
async def oauth_callback(
    code: str = Query(...),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_user_id),
    clinic_id: int = Depends(get_clinic_id)
):
    """
    Callback de Google que recibe el código, lo intercambia por tokens
    y los vincula permanentemente al odontólogo en la base de datos.
    """
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
        
        return {
            "status": "success",
            "message": f"¡Calendario vinculado con éxito ({google_email})! Ya puedes cerrar esta ventana."
        }
    except Exception as e:
        logger.error(f"Error in OAuth callback for dentist {user_id}: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=400, detail="Error vinculando cuenta de Google")
