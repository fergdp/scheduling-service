import logging
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from dependencies import ALGORITHM, SECRET_KEY_BYTES


def _token_con_sub(user_id, sub):
    """JWT de verdad, firmado con la clave del servicio (test_token_y_cifrado.py)."""
    return jwt.encode(
        {
            "user_id": user_id, "sub": sub, "clinic_id": 1, "roles": ["ROLE_ADMIN"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        SECRET_KEY_BYTES, algorithm=ALGORITHM,
    )


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


# ---------------------------------------------------------------------------
# Logging del usuario — issue #307
# ---------------------------------------------------------------------------
#
# El middleware y el exception_handler leen la cookie `token` A MANO (corren fuera de la
# inyección de dependencias de FastAPI), así que pisar `get_current_user` con
# `dependency_overrides` —como hace la fixture `client`— no alcanza para probarlos: hace falta
# una cookie con un JWT de verdad, mismo motivo que `test_rate_limiter.py`.

def test_log_requests_loguea_el_user_id_no_el_nombre_de_usuario(client, caplog):
    """
    El middleware logueaba `sub` (el nombre de usuario, que en pacientes puede coincidir con
    el mail) en CADA pedido, mandado a Grafana/Loki. `user_id` alcanza igual para
    correlacionar sin identificar a la persona.
    """
    client.cookies.set("token", _token_con_sub(user_id=4242, sub="nombre.de.usuario.privado"))

    with caplog.at_level(logging.INFO):
        res = client.get("/clinic-scheduling-api/v1/appointments/upcoming")
    assert res.status_code == 200

    logueado = "\n".join(
        r.getMessage() for r in caplog.records if "Incoming request" in r.getMessage()
    )
    assert logueado, "no se logueó ningún 'Incoming request'"
    assert "4242" in logueado
    assert "nombre.de.usuario.privado" not in logueado


def test_exception_handler_loguea_el_user_id_no_el_nombre_de_usuario(
    client_sin_reraise, caplog, monkeypatch,
):
    """Mismo criterio que log_requests, para el logueo de un 500 sin manejar."""
    import routers.appointments as ra
    def _explota(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ra, "_utcnow_naive", _explota)

    client_sin_reraise.cookies.set(
        "token", _token_con_sub(user_id=4242, sub="nombre.de.usuario.privado"),
    )

    with caplog.at_level(logging.INFO):
        res = client_sin_reraise.get("/clinic-scheduling-api/v1/appointments/upcoming")
    assert res.status_code == 500

    logueado = "\n".join(
        r.getMessage() for r in caplog.records if "Unhandled exception" in r.getMessage()
    )
    assert logueado, "no se logueó ninguna 'Unhandled exception'"
    assert "4242" in logueado
    assert "nombre.de.usuario.privado" not in logueado
