import pytest
from dependencies import get_clinic_id, get_user_id
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
