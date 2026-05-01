import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
from utils.google_calendar import (
    get_google_auth_url, exchange_code_for_tokens, get_google_user_email,
    get_free_busy, get_calendar_service,
)

def test_get_google_auth_url():
    """Verifica que la URL generada contenga los parámetros necesarios, incluido el state."""
    url = get_google_auth_url("test_state_value")
    assert "access_type=offline" in url
    assert "response_type=code" in url
    assert "scope=" in url
    assert "state=test_state_value" in url

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


# ---------------------------------------------------------------------------
# get_free_busy: errores silenciosos (issue #32)
# ---------------------------------------------------------------------------

def _make_freebusy_service(response):
    """Helper: construye un mock de service.freebusy().query().execute() con la respuesta dada."""
    service = MagicMock()
    service.freebusy().query().execute.return_value = response
    return service


def test_get_free_busy_returns_busy_slots_normally():
    """Caso happy path: Google devuelve busy[] sin errors → retorna la lista."""
    busy = [{"start": "2027-01-01T10:00:00Z", "end": "2027-01-01T11:00:00Z"}]
    service = _make_freebusy_service({"calendars": {"primary": {"busy": busy}}})
    result = get_free_busy(service, "primary", datetime(2027, 1, 1), datetime(2027, 1, 2))
    assert result == busy


def test_get_free_busy_returns_empty_when_no_busy_slots():
    """Sin busy[] (calendario libre) retorna lista vacía — comportamiento legítimo."""
    service = _make_freebusy_service({"calendars": {"primary": {"busy": []}}})
    result = get_free_busy(service, "primary", datetime(2027, 1, 1), datetime(2027, 1, 2))
    assert result == []


def test_get_free_busy_raises_when_google_returns_errors():
    """
    Si Google responde 200 OK con errors[] para el calendario, antes retornábamos []
    silenciosamente y el paciente veía slots libres ocupados. Ahora debe lanzar.
    """
    response = {
        "calendars": {
            "primary": {
                "errors": [{"domain": "global", "reason": "notFound"}],
                "busy": []
            }
        }
    }
    service = _make_freebusy_service(response)
    with pytest.raises(RuntimeError, match="freebusy errors"):
        get_free_busy(service, "primary", datetime(2027, 1, 1), datetime(2027, 1, 2))


# ---------------------------------------------------------------------------
# get_calendar_service: validación post-refresh (issue #32)
# ---------------------------------------------------------------------------

@patch("utils.google_calendar.build")
@patch("utils.google_calendar.Credentials")
def test_get_calendar_service_raises_when_invalid_after_refresh(mock_creds_cls, mock_build):
    """
    Si refresh() no lanza pero deja creds.valid=False, el código antes seguía adelante
    y llamaba a la API con un token inválido. Ahora debe lanzar RuntimeError.
    """
    creds = MagicMock()
    creds.expired = True
    creds.refresh_token = "refresh_xyz"
    creds.valid = False  # post-refresh igual queda inválido
    mock_creds_cls.return_value = creds

    with pytest.raises(RuntimeError, match="invalid after refresh"):
        get_calendar_service("access", "refresh_xyz", datetime(2027, 1, 1))

    creds.refresh.assert_called_once()
    mock_build.assert_not_called()


@patch("utils.google_calendar.build")
@patch("utils.google_calendar.Credentials")
def test_get_calendar_service_succeeds_when_valid_after_refresh(mock_creds_cls, mock_build):
    """Refresh exitoso (creds.valid=True) construye el service normalmente."""
    creds = MagicMock()
    creds.expired = True
    creds.refresh_token = "refresh_xyz"
    creds.valid = True
    mock_creds_cls.return_value = creds
    mock_build.return_value = MagicMock()

    service = get_calendar_service("access", "refresh_xyz", datetime(2027, 1, 1))

    creds.refresh.assert_called_once()
    mock_build.assert_called_once()
    assert service is mock_build.return_value


@patch("utils.google_calendar.build")
@patch("utils.google_calendar.Credentials")
def test_get_calendar_service_skips_refresh_when_not_expired(mock_creds_cls, mock_build):
    """Si las creds no están expiradas, no se hace refresh."""
    creds = MagicMock()
    creds.expired = False
    creds.refresh_token = "refresh_xyz"
    mock_creds_cls.return_value = creds
    mock_build.return_value = MagicMock()

    get_calendar_service("access", "refresh_xyz", datetime(2027, 1, 1))

    creds.refresh.assert_not_called()
    mock_build.assert_called_once()
