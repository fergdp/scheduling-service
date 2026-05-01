import pytest
from unittest.mock import patch, MagicMock

_VALID_STATE = "test_oauth_state_abc123xyz"


def test_get_oauth_url(client):
    """Verifica que se genere una URL de Google válida y se setee la cookie de state."""
    response = client.get("/clinic-scheduling-api/v1/oauth/url")
    assert response.status_code == 200
    assert "accounts.google.com" in response.json()["auth_url"]
    assert "state=" in response.json()["auth_url"]
    assert "oauth_state" in response.cookies


@patch("routers.oauth.exchange_code_for_tokens")
@patch("routers.oauth.get_google_user_email")
def test_oauth_callback_success(mock_email, mock_exchange, client):
    """Verifica el guardado exitoso de tokens tras el callback de Google."""
    mock_exchange.return_value = {
        "access_token": "access_123",
        "refresh_token": "refresh_123",
        "token_expiry": None
    }
    mock_email.return_value = "dentista@gmail.com"

    response = client.get(
        f"/clinic-scheduling-api/v1/oauth/callback?code=fake_code_123&state={_VALID_STATE}",
        cookies={"oauth_state": _VALID_STATE}
    )

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Google Calendar Vinculado" in response.text
    assert "dentista@gmail.com" in response.text


@patch("routers.oauth.exchange_code_for_tokens")
@patch("routers.oauth.get_google_user_email")
def test_oauth_callback_missing_state_cookie(mock_email, mock_exchange, client):
    """Callback sin cookie de state debe devolver 400 (posible CSRF)."""
    mock_exchange.return_value = {"access_token": "a", "refresh_token": "r", "token_expiry": None}
    mock_email.return_value = "x@gmail.com"

    # No se envía la cookie oauth_state
    response = client.get(
        f"/clinic-scheduling-api/v1/oauth/callback?code=fake_code&state={_VALID_STATE}"
    )
    assert response.status_code == 400
    assert "CSRF" in response.json()["detail"] or "state" in response.json()["detail"].lower()


@patch("routers.oauth.exchange_code_for_tokens")
@patch("routers.oauth.get_google_user_email")
def test_oauth_callback_wrong_state(mock_email, mock_exchange, client):
    """Callback con state diferente al de la cookie debe devolver 400."""
    mock_exchange.return_value = {"access_token": "a", "refresh_token": "r", "token_expiry": None}
    mock_email.return_value = "x@gmail.com"

    response = client.get(
        "/clinic-scheduling-api/v1/oauth/callback?code=fake_code&state=attacker_state",
        cookies={"oauth_state": _VALID_STATE}
    )
    assert response.status_code == 400
