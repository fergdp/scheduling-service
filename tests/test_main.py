import pytest
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def test_root_endpoint(client):
    """Verifica que el servicio responda correctamente en la raíz."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "Scheduling Service"
    assert response.json()["status"] == "Healthy"


# ---------------------------------------------------------------------------
# Lifespan handler — issue #33
# ---------------------------------------------------------------------------

def test_lifespan_warmup_pings_both_engines_and_disposes_on_shutdown():
    """
    Startup: el lifespan debe ejecutar SELECT 1 contra `engine` y `clinic_engine`
    para detectar DBs caídas en boot, no en la primera request.
    Shutdown: ambos pools se dispose() para cerrar conexiones limpias al SIGTERM.
    """
    fake_engine_conn = MagicMock()
    fake_engine = MagicMock()
    fake_engine.connect.return_value.__enter__.return_value = fake_engine_conn

    fake_clinic_conn = MagicMock()
    fake_clinic_engine = MagicMock()
    fake_clinic_engine.connect.return_value.__enter__.return_value = fake_clinic_conn

    with patch.object(main, "engine", fake_engine), \
         patch.object(main, "clinic_engine", fake_clinic_engine):
        test_app = FastAPI(lifespan=main.lifespan)
        with TestClient(test_app):
            # TestClient context manager dispara startup
            fake_engine.connect.assert_called_once()
            fake_clinic_engine.connect.assert_called_once()
            fake_engine_conn.execute.assert_called_once()
            fake_clinic_conn.execute.assert_called_once()
        # Salida del context dispara shutdown
        fake_engine.dispose.assert_called_once()
        fake_clinic_engine.dispose.assert_called_once()


def test_lifespan_fails_fast_when_db_unreachable():
    """
    Si una DB es inalcanzable en startup, lifespan debe propagar la excepción —
    el servicio NO debe arrancar sirviendo requests con pool roto.
    """
    fake_engine = MagicMock()
    fake_engine.connect.side_effect = ConnectionError("DB unreachable")
    fake_clinic_engine = MagicMock()

    with patch.object(main, "engine", fake_engine), \
         patch.object(main, "clinic_engine", fake_clinic_engine):
        test_app = FastAPI(lifespan=main.lifespan)
        with pytest.raises(ConnectionError, match="DB unreachable"):
            with TestClient(test_app):
                pass
