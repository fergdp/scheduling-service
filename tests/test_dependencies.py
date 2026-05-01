import pytest
from unittest.mock import MagicMock, patch
from dependencies import get_clinic_id, get_user_id, get_db
from fastapi import HTTPException

def test_get_clinic_id_unauthorized():
    """Verifica que get_clinic_id lance 401 si no hay payload."""
    with pytest.raises(HTTPException) as exc:
        get_clinic_id(None)
    assert exc.value.status_code == 401

def test_get_user_id_unauthorized():
    """Verifica que get_user_id lance 401 si no hay payload."""
    with pytest.raises(HTTPException) as exc:
        get_user_id(None)
    assert exc.value.status_code == 401

def test_get_clinic_id_missing_field():
    """Verifica error si falta clinic_id en el payload."""
    with pytest.raises(HTTPException) as exc:
        get_clinic_id({"user_id": 1})
    assert exc.value.status_code == 401

def test_get_user_id_missing_field():
    """Verifica error si falta user_id en el payload."""
    with pytest.raises(HTTPException) as exc:
        get_user_id({"clinic_id": 1})
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# get_db: rollback + propagación correcta de excepciones (issue #32)
# ---------------------------------------------------------------------------

def _consume_get_db_with_exception(exc_to_raise):
    """
    Consume el generador de get_db() con un mock de SessionLocal y simula que el
    request handler lanza exc_to_raise. Devuelve el mock de la sesión para
    inspeccionar rollback() y close().
    """
    mock_session = MagicMock()
    with patch("dependencies.SessionLocal", return_value=mock_session):
        gen = get_db()
        db = next(gen)
        assert db is mock_session
        with pytest.raises(type(exc_to_raise)) as exc_info:
            gen.throw(exc_to_raise)
        return mock_session, exc_info.value


def test_get_db_rollbacks_on_exception():
    """Cualquier excepción durante el request dispara rollback antes de close."""
    session, _ = _consume_get_db_with_exception(RuntimeError("boom"))
    session.rollback.assert_called_once()
    session.close.assert_called_once()


def test_get_db_does_not_swallow_http_exception():
    """
    HTTPException(404) lanzada por un endpoint NO debe convertirse en HTTP 500.
    Antes el except la envolvía en HTTPException(500, "Database connection error").
    """
    original = HTTPException(status_code=404, detail="Appointment not found")
    session, raised = _consume_get_db_with_exception(original)
    assert raised is original
    assert raised.status_code == 404
    assert raised.detail == "Appointment not found"
    session.rollback.assert_called_once()


def test_get_db_reraises_original_exception_type():
    """Una excepción no-HTTP (ej. SQLAlchemyError) propaga como tal, no como HTTP 500."""
    original = ValueError("invalid input")
    _, raised = _consume_get_db_with_exception(original)
    assert raised is original


def test_get_db_closes_session_on_success():
    """Sin excepción, close() se llama igual en el finally."""
    mock_session = MagicMock()
    with patch("dependencies.SessionLocal", return_value=mock_session):
        gen = get_db()
        db = next(gen)
        assert db is mock_session
        # Cerramos el generador limpiamente (sin excepción)
        with pytest.raises(StopIteration):
            next(gen)
    mock_session.close.assert_called_once()
    mock_session.rollback.assert_not_called()
