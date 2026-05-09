import pytest
from unittest.mock import MagicMock, patch
import dependencies
from dependencies import get_clinic_id, get_user_id, get_db
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def clear_clinic_cache():
    dependencies._user_clinic_cache.clear()
    yield
    dependencies._user_clinic_cache.clear()


def _patch_db_returning(clinic_id):
    fake_session = MagicMock()
    if clinic_id is None:
        fake_session.execute.return_value.first.return_value = None
    else:
        fake_session.execute.return_value.first.return_value = (clinic_id,)
    return patch.object(dependencies, "ClinicSessionLocal", return_value=fake_session)


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
    """Non-admin sin clinic_id en payload → 401 (token roto / sospechoso)."""
    with pytest.raises(HTTPException) as exc:
        get_clinic_id({"user_id": 1})
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Admin sin clinica: 403, NO 401 — preserva la sesion en el frontend
# (apiFactory.js solo redirige al login con sessionExpired=true cuando es 401)
# ---------------------------------------------------------------------------

def test_get_clinic_id_admin_without_clinic_in_jwt_returns_403():
    """Admin con clinic_id=0 (ROLE_ADMIN cross-clinic) → 403, no 401."""
    payload = {"user_id": 1, "clinic_id": 0, "roles": ["ROLE_ADMIN"]}
    with pytest.raises(HTTPException) as exc:
        get_clinic_id(payload)
    assert exc.value.status_code == 403
    assert "clinica" in exc.value.detail.lower()


def test_get_clinic_id_admin_without_clinic_no_role_prefix_returns_403():
    """Same como el anterior pero con role sin prefijo ROLE_ (defensa por si Spring cambia)."""
    payload = {"user_id": 1, "clinic_id": 0, "roles": ["ADMIN"]}
    with pytest.raises(HTTPException) as exc:
        get_clinic_id(payload)
    assert exc.value.status_code == 403


def test_get_clinic_id_admin_with_clinic_in_jwt_but_null_in_db_returns_403():
    """Admin con clinic_id en JWT pero NULL en DB (cross-clinic real) → 403."""
    payload = {"user_id": 1, "clinic_id": 99, "roles": ["ROLE_ADMIN"]}
    with _patch_db_returning(None):
        with pytest.raises(HTTPException) as exc:
            get_clinic_id(payload)
        assert exc.value.status_code == 403


def test_get_clinic_id_admin_with_valid_clinic_passes():
    """Admin que SI tiene clinic asignada pasa el cross-check normalmente."""
    payload = {"user_id": 1, "clinic_id": 1, "roles": ["ROLE_ADMIN"]}
    with _patch_db_returning(1):
        assert get_clinic_id(payload) == 1


def test_get_clinic_id_non_admin_user_not_in_db_still_401():
    """Regresion: non-admin sin row en DB sigue siendo 401 fail-close (no 403)."""
    payload = {"user_id": 999, "clinic_id": 1, "roles": ["ROLE_DENTIST"]}
    with _patch_db_returning(None):
        with pytest.raises(HTTPException) as exc:
            get_clinic_id(payload)
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Defense-in-depth (#82 H1): cross-check JWT.clinic_id vs DB
# ---------------------------------------------------------------------------

def test_get_clinic_id_authorizes_when_jwt_matches_db():
    with _patch_db_returning(1):
        assert get_clinic_id({"user_id": 99, "clinic_id": 1}) == 1


def test_get_clinic_id_fails_close_when_jwt_clinic_mismatches_db():
    with _patch_db_returning(1):
        with pytest.raises(HTTPException) as exc:
            get_clinic_id({"user_id": 99, "clinic_id": 2})
        assert exc.value.status_code == 401


def test_get_clinic_id_fails_close_when_user_not_in_db():
    with _patch_db_returning(None):
        with pytest.raises(HTTPException) as exc:
            get_clinic_id({"user_id": 999, "clinic_id": 1})
        assert exc.value.status_code == 401


def test_get_clinic_id_rejects_token_without_user_id():
    with pytest.raises(HTTPException) as exc:
        get_clinic_id({"clinic_id": 1})
    assert exc.value.status_code == 401


def test_db_lookup_is_cached_within_ttl():
    fake_session = MagicMock()
    fake_session.execute.return_value.first.return_value = (1,)
    with patch.object(dependencies, "ClinicSessionLocal", return_value=fake_session):
        get_clinic_id({"user_id": 99, "clinic_id": 1})
        get_clinic_id({"user_id": 99, "clinic_id": 1})
    assert fake_session.execute.call_count == 1


def test_resolve_db_clinic_id_uses_clinic_db_not_scheduling_db():
    """
    Regresión: el cross-check #82 H1 debe hacer SELECT contra la DB de dental-clinic
    (donde vive `users`), NO contra la DB de scheduling. Si alguien vuelve a usar
    SessionLocal acá, scheduling tira 1146 "Table users doesn't exist" en prod.
    """
    fake_clinic_session = MagicMock()
    fake_clinic_session.execute.return_value.first.return_value = (7,)
    fake_scheduling_session = MagicMock()

    with patch.object(dependencies, "ClinicSessionLocal", return_value=fake_clinic_session), \
         patch.object(dependencies, "SessionLocal", return_value=fake_scheduling_session):
        assert get_clinic_id({"user_id": 42, "clinic_id": 7}) == 7

    fake_clinic_session.execute.assert_called_once()
    fake_scheduling_session.execute.assert_not_called()

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
