import os
import logging
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid"
]


def get_google_user_email(credentials) -> str:
    service = build("oauth2", "v2", credentials=credentials)
    user_info = service.userinfo().get().execute()
    return user_info.get("email")


def get_google_auth_url(state: str) -> str:
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
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    return auth_url


def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
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
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        expiry=token_expiry
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


def get_free_busy(service, calendar_id: str, start_time: datetime, end_time: datetime) -> list:
    body = {
        "timeMin": start_time.isoformat() + "Z",
        "timeMax": end_time.isoformat() + "Z",
        "items": [{"id": calendar_id}]
    }
    query = service.freebusy().query(body=body).execute()
    return query.get("calendars", {}).get(calendar_id, {}).get("busy", [])


def create_google_event(
    service,
    calendar_id: str,
    start_time: datetime,
    end_time: datetime,
    summary: str,
    description: str,
    attendees: List[str] = []
) -> str:
    """Crea un evento en Google Calendar. Devuelve el event_id."""
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time.isoformat() + "Z", "timeZone": "UTC"},
        "end":   {"dateTime": end_time.isoformat()   + "Z", "timeZone": "UTC"},
    }
    if attendees:
        event["attendees"] = [{"email": email} for email in attendees]
        # sendUpdates="all" manda la invitación de Google Calendar al paciente por email
    result = service.events().insert(
        calendarId=calendar_id,
        body=event,
        sendUpdates="all" if attendees else "none"
    ).execute()
    return result.get("id")


def update_google_event(
    service,
    calendar_id: str,
    event_id: str,
    summary: str,
    start_time: datetime,
    end_time: datetime,
    description: str,
    attendees: List[str] = []
) -> None:
    """Actualiza un evento existente en Google Calendar."""
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time.isoformat() + "Z", "timeZone": "UTC"},
        "end":   {"dateTime": end_time.isoformat()   + "Z", "timeZone": "UTC"},
    }
    if attendees:
        event["attendees"] = [{"email": email} for email in attendees]
    service.events().update(
        calendarId=calendar_id,
        eventId=event_id,
        body=event,
        sendUpdates="all" if attendees else "none"
    ).execute()


def delete_google_event(service, calendar_id: str, event_id: str) -> None:
    """Elimina un evento de Google Calendar y notifica a los asistentes."""
    service.events().delete(
        calendarId=calendar_id,
        eventId=event_id,
        sendUpdates="all"
    ).execute()


