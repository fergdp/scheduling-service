import pytest
from unittest.mock import patch, MagicMock

def test_get_oauth_url(client):
    """Verifica que se genere una URL de Google válida."""
    response = client.get("/clinic-scheduling-api/v1/oauth/url")
    assert response.status_code == 200
    assert "accounts.google.com" in response.json()["auth_url"]

@patch("routers.oauth.exchange_code_for_tokens")
@patch("routers.oauth.get_google_user_email")
def test_oauth_callback_success(mock_email, mock_exchange, client):
    """Verifica el guardado exitoso de tokens tras el callback de Google."""
    # Mocks
    mock_exchange.return_value = {
        "access_token": "access_123",
        "refresh_token": "refresh_123",
        "token_expiry": None
    }
    mock_email.return_value = "dentista@gmail.com"
    
    response = client.get("/clinic-scheduling-api/v1/oauth/callback?code=fake_code_123")
    
    assert response.status_code == 200
    assert "vinculado con éxito" in response.json()["message"]
    assert "dentista@gmail.com" in response.json()["message"]
