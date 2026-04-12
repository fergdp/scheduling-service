import os
import logging
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Configuración de Google OAuth2 (vía .env)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

# Scopes necesarios (mínimo privilegio): leer disponibilidad, crear eventos e identificar la cuenta.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid"
]

def get_google_user_email(credentials) -> str:
    """Obtiene el email del usuario autenticado en Google."""
    service = build("oauth2", "v2", credentials=credentials)
    user_info = service.userinfo().get().execute()
    return user_info.get("email")

def get_google_auth_url() -> str:
    """Genera la URL para que el odontólogo autorice a la app."""
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI]
        }
    }
    
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    
    # Offline para obtener refresh_token; prompt=consent para asegurar que nos lo den siempre.
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return auth_url

def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    """Intercambia el código de autorización de Google por tokens de acceso."""
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token"
        }
    }
    
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    flow.fetch_token(code=code)
    
    credentials = flow.credentials
    return {
        "access_token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_expiry": credentials.expiry
    }

def get_calendar_service(access_token: str, refresh_token: str, token_expiry: datetime):
    """Construye el cliente de la API de Google Calendar usando los tokens."""
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        expiry=token_expiry
    )
    
    # Si el token expiró, lo refresca automáticamente
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        
    return build("calendar", "v3", credentials=creds)

def get_free_busy(service, calendar_id: str, start_time: datetime, end_time: datetime):
    """Consulta los bloques ocupados en el calendario de Google."""
    body = {
        "timeMin": start_time.isoformat() + "Z",
        "timeMax": end_time.isoformat() + "Z",
        "items": [{"id": calendar_id}]
    }
    
    query = service.freebusy().query(body=body).execute()
    return query.get("calendars", {}).get(calendar_id, {}).get("busy", [])

def create_google_event(service, calendar_id: str, start_time: datetime, end_time: datetime, summary: str, description: str):
    """Crea un evento en el calendario de Google."""
    event = {
        'summary': summary,
        'description': description,
        'start': {
            'dateTime': start_time.isoformat() + "Z",
            'timeZone': 'UTC',
        },
        'end': {
            'dateTime': end_time.isoformat() + "Z",
            'timeZone': 'UTC',
        },
    }
    
    event = service.events().insert(calendarId=calendar_id, body=event).execute()
    return event.get('id')
