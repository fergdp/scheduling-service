# Varios módulos validan env vars al import (raise ValueError si faltan):
# dependencies.py → JWT_SECRET_KEY, DATABASE_URL, CLINIC_DATABASE_URL
# utils/crypto.py → FERNET_KEY
# setdefault sólo aplica si no están seteadas — el CI / dev real las pisa con
# las suyas. Esto hace que `pytest tests/` corra en cualquier entorno limpio
# sin tener que exportar nada antes.
import os
os.environ.setdefault("JWT_SECRET_KEY", "dGVzdC1zZWNyZXQ=")  # base64 de "test-secret"
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("CLINIC_DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_KEY", "mgp552Y1rs_rkZO4lFIZKyStcqVmND1nFWSMZX9dCys=")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker
from main import app
from dependencies import get_db, get_current_user, get_clinic_id
from models import Base

# Setup SQLite in-memory for testing
SQLALCHEMY_DATABASE_URL = "sqlite://"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Dependency override for DB
def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

# Dependency override for Auth (Simulate a logged-in user)
def override_get_current_user():
    return {"user_id": 1, "clinic_id": 1, "roles": ["DENTIST", "ADMIN"]}

@pytest.fixture(autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    # Reset rate limiter counters so each test starts from zero
    from routers.appointments import limiter as apts_limiter
    from main import limiter as main_limiter
    apts_limiter._storage.reset()
    main_limiter._storage.reset()
    yield
    Base.metadata.drop_all(bind=engine)

def _make_client(user_override):
    """
    Helper que crea un TestClient con las dependency overrides dadas
    y las restaura al estado previo al salir — no llama a .clear() global
    para no interferir con otros fixtures activos en el mismo test.
    """
    # Bypass del cross-check JWT.clinic_id vs DB (#82 H1) para tests de endpoints:
    # devolvemos el clinic_id del JWT mock directamente, los tests del cross-check
    # viven en test_dependencies.py.
    def override_get_clinic_id():
        payload = user_override()
        return int(payload["clinic_id"])

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = user_override
    app.dependency_overrides[get_clinic_id] = override_get_clinic_id
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def client():
    """Usuario DENTIST + ADMIN (user_id=1). Fixture principal de tests."""
    yield from _make_client(override_get_current_user)


@pytest.fixture
def patient_client():
    """Usuario PATIENT (user_id=10)."""
    def _patient():
        return {"user_id": 10, "clinic_id": 1, "roles": ["PATIENT"]}
    yield from _make_client(_patient)


@pytest.fixture
def other_dentist_client():
    """Dentista diferente al asignado al turno (user_id=99)."""
    def _dentist():
        return {"user_id": 99, "clinic_id": 1, "roles": ["DENTIST"]}
    yield from _make_client(_dentist)
