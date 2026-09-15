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


@pytest.fixture(autouse=True)
def dentistas_de_la_clinica_1(monkeypatch):
    """
    Los endpoints validan que el odontólogo del turno sea de la clínica (#286) con un SELECT
    a `users` de dental-clinic, tabla que la base de tests no tiene. Por defecto todo
    odontólogo es de la clínica 1 (la de las fixtures); el test que prueba la validación
    reemplaza esto con un mapa explícito.
    """
    import routers.appointments as ra
    monkeypatch.setattr(ra, "_clinic_of_user", lambda user_id: 1)

def _sembrar_csrf(c):
    """
    Deja al cliente con la cookie XSRF-TOKEN y el header X-XSRF-TOKEN puestos, que es
    como llega SIEMPRE un browser real: los dos SPA usan axios con `withXSRFToken: true`,
    que lee la cookie y arma el header solo.

    Hace falta desde el #262: antes de ese arreglo el middleware de CSRF eximía TODAS las
    rutas, así que los tests podían mutar estado sin token y pasaban igual. Con el CSRF
    prendido de verdad, un cliente sin token es un cliente que no existe en producción.

    El token se pone a mano en vez de pedirlo con un GET a una ruta exenta para no gastar
    el rate limit de la raíz (5/minuto) ni depender del orden de las requests del test.
    Que la protección funcione lo prueba test_security.py, con clientes SIN esto.
    """
    from csrf_middleware import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, generate_csrf_token

    token = generate_csrf_token()
    c.cookies.set(CSRF_COOKIE_NAME, token)
    c.headers[CSRF_HEADER_NAME] = token


def _make_client(user_override, raise_server_exceptions=True, csrf=True):
    """
    Helper que crea un TestClient con las dependency overrides dadas
    y las restaura al estado previo al salir — no llama a .clear() global
    para no interferir con otros fixtures activos en el mismo test.

    `raise_server_exceptions=False` hace que el cliente devuelva la respuesta de
    error en vez de re-lanzar la excepción del servidor dentro del test, que es lo
    que ve un browser. Lo usa test_security.py; ver el docstring de esa fixture.

    `csrf=False` entrega el cliente SIN token, que es lo que necesita test_security.py
    para poder probar el rechazo.
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
        with TestClient(app, raise_server_exceptions=raise_server_exceptions) as c:
            if csrf:
                _sembrar_csrf(c)
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def client():
    """Usuario DENTIST + ADMIN (user_id=1). Fixture principal de tests."""
    yield from _make_client(override_get_current_user)


@pytest.fixture
def client_sin_reraise():
    """
    Igual que `client` pero devolviendo las respuestas de error en vez de re-lanzar
    la excepción del servidor — o sea, lo que ve un browser (issue #262).

    ⚠️ No es cosmético. Starlette NO pasa por los exception handlers de FastAPI lo
    que se levanta dentro de un BaseHTTPMiddleware, así que un `raise HTTPException(403)`
    ahí sale 500 al cliente. Con `raise_server_exceptions=True` (el default) el test ve
    el 403 que nunca llegó a la red y pasa en verde: el instrumento miente justo en la
    dirección que oculta el bug. Los tests de CSRF usan esta fixture.

    Viene además SIN token CSRF (`csrf=False`), al revés que `client`: es el cliente con
    el que se prueba el rechazo.
    """
    yield from _make_client(
        override_get_current_user, raise_server_exceptions=False, csrf=False
    )


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


@pytest.fixture
def receptionist_client():
    """Recepcionista de la clínica 1 (user_id=50), sin ningún otro rol."""
    def _receptionist():
        return {"user_id": 50, "clinic_id": 1, "roles": ["RECEPTIONIST"]}
    yield from _make_client(_receptionist)


@pytest.fixture
def other_clinic_client():
    """Recepcionista de OTRA clínica (clinic_id=2, user_id=60): para el aislamiento multi-tenant."""
    def _receptionist():
        return {"user_id": 60, "clinic_id": 2, "roles": ["RECEPTIONIST"]}
    yield from _make_client(_receptionist)
