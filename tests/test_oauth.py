import pytest
from unittest.mock import patch


def _get_valid_state(client) -> tuple[str, str]:
    """Obtiene un state JWT válido llamando a /url, devuelve (state_jwt, cookie_value)."""
    url_resp = client.get("/clinic-scheduling-api/v1/oauth/url")
    assert url_resp.status_code == 200
    state = url_resp.json()["auth_url"].split("state=")[1].split("&")[0]
    cookie = url_resp.cookies.get("oauth_state", state)
    return state, cookie


def _fijar_cookie_state(client, valor: str) -> None:
    """
    Deja EXACTAMENTE una cookie `oauth_state` en el cliente.

    No se usa el `cookies=` per-request de httpx —el que la propia librería deprecó por
    "the expected behaviour on cookie persistence is ambiguous"— porque **no pisa** la
    cookie que ya dejó la respuesta de /url: quedan las dos en el jar, con distinto
    domain, y el header sale `oauth_state=A; oauth_state=B`. Cuál gana depende del orden
    del jar, que cambia al agregar cualquier otra cookie.

    Medido al sembrar la cookie de CSRF del #262: con el jar vacío el header salía
    `BUENA; ADULTERADA` y ganaba la adulterada, así que el test de state adulterado
    pasaba; con una cookie más en el jar el orden se dio vuelta a
    `ADULTERADA; BUENA` y el mismo test empezó a dar 200. O sea que estaba pasando
    por el orden accidental del jar, no porque el servidor rechazara nada.
    """
    client.cookies.delete("oauth_state")
    client.cookies.set("oauth_state", valor)


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
    """Callback con state JWT válido y cookie coincidente vincula la cuenta correctamente."""
    mock_exchange.return_value = {
        "access_token": "access_123",
        "refresh_token": "refresh_123",
        "token_expiry": None
    }
    mock_email.return_value = "dentista@gmail.com"

    state, cookie = _get_valid_state(client)

    _fijar_cookie_state(client, cookie)
    response = client.get(
        f"/clinic-scheduling-api/v1/oauth/callback?code=fake_code_123&state={state}"
    )

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Google Calendar Vinculado" in response.text
    assert "dentista@gmail.com" in response.text


@patch("routers.oauth.exchange_code_for_tokens")
@patch("routers.oauth.get_google_user_email")
def test_oauth_callback_without_state_cookie(mock_email, mock_exchange, client):
    """Callback sin cookie de state es válido — la cookie es defensa secundaria opcional."""
    mock_exchange.return_value = {"access_token": "a", "refresh_token": "r", "token_expiry": None}
    mock_email.return_value = "x@gmail.com"

    state, _ = _get_valid_state(client)
    # Sin este delete el test no prueba lo que dice: /url ya dejó la cookie BUENA en el
    # jar y el cliente la manda igual, así que se estaba ejercitando el camino "cookie
    # que coincide", no el de "sin cookie".
    client.cookies.delete("oauth_state")

    response = client.get(
        f"/clinic-scheduling-api/v1/oauth/callback?code=fake_code&state={state}"
    )
    # Sin cookie el JWT sigue siendo válido — se acepta
    assert response.status_code == 200


@patch("routers.oauth.exchange_code_for_tokens")
@patch("routers.oauth.get_google_user_email")
def test_oauth_callback_wrong_state_cookie(mock_email, mock_exchange, client):
    """Cookie de state diferente al query param debe devolver 400 (CSRF)."""
    mock_exchange.return_value = {"access_token": "a", "refresh_token": "r", "token_expiry": None}
    mock_email.return_value = "x@gmail.com"

    state, _ = _get_valid_state(client)
    tampered_cookie = state[:-4] + "xxxx"  # cookie alterada

    _fijar_cookie_state(client, tampered_cookie)
    response = client.get(
        f"/clinic-scheduling-api/v1/oauth/callback?code=fake_code&state={state}"
    )
    assert response.status_code == 400
    assert "CSRF" in response.json()["detail"] or "state" in response.json()["detail"].lower()


def test_oauth_callback_invalid_state_jwt(client):
    """State que no es un JWT válido debe devolver 400."""
    _fijar_cookie_state(client, "not_a_jwt_at_all")
    response = client.get(
        "/clinic-scheduling-api/v1/oauth/callback?code=fake_code&state=not_a_jwt_at_all"
    )
    assert response.status_code == 400
