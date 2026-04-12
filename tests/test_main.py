import pytest

def test_root_endpoint(client):
    """Verifica que el servicio responda correctamente en la raíz."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "Scheduling Service"
    assert response.json()["status"] == "Healthy"
