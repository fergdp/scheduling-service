from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
import os

os.environ.setdefault("JWT_SECRET_KEY", "dGVzdC1zZWNyZXQta2V5LWZvci11bml0LXRlc3Rpbmctb25seQ==")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from main import app
from dependencies import get_db

client = TestClient(app, raise_server_exceptions=False)


def test_liveness_returns_200():
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readiness_returns_200_when_db_up():
    mock_db = MagicMock()
    mock_db.execute.return_value.scalar.return_value = 1

    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert response.json()["checks"]["database"] == "ok"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_readiness_returns_503_when_db_down():
    def broken_db():
        db = MagicMock()
        db.execute.side_effect = OperationalError("conn", {}, Exception("DB down"))
        yield db

    app.dependency_overrides[get_db] = broken_db
    try:
        response = client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["database"] != "ok"
    finally:
        app.dependency_overrides.pop(get_db, None)
