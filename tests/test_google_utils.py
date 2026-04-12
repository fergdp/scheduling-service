import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
from utils.google_calendar import get_google_auth_url, exchange_code_for_tokens, get_google_user_email

def test_get_google_auth_url():
    """Verifica que la URL generada contenga los parámetros necesarios."""
    url = get_google_auth_url()
    assert "access_type=offline" in url
    assert "response_type=code" in url
    assert "scope=" in url

@patch("utils.google_calendar.Flow.from_client_config")
def test_exchange_code_success(mock_flow_init):
    """Verifica el intercambio de código por tokens simula la respuesta de Google."""
    mock_flow = MagicMock()
    mock_flow_init.return_value = mock_flow
    
    mock_creds = MagicMock()
    mock_creds.token = "access_123"
    mock_creds.refresh_token = "refresh_123"
    mock_creds.expiry = datetime.now()
    mock_flow.credentials = mock_creds
    
    tokens = exchange_code_for_tokens("fake_code")
    
    assert tokens["access_token"] == "access_123"
    assert tokens["refresh_token"] == "refresh_123"

@patch("utils.google_calendar.build")
def test_get_google_user_email(mock_build):
    """Verifica la obtención del email simulando la API de Google."""
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    mock_service.userinfo().get().execute.return_value = {"email": "test@gmail.com"}
    
    email = get_google_user_email(MagicMock())
    assert email == "test@gmail.com"
