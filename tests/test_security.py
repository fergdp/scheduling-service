import pytest

def test_csrf_protection_success_with_matching_tokens(client):
    """Verifica que el CSRF pase si los tokens coinciden."""
    # 1. Obtener el token de la cookie en el root
    get_res = client.get("/")
    csrf_token = get_res.cookies.get("XSRF-TOKEN")
    
    # 2. Enviar POST con el mismo token en la cabecera
    headers = {"X-XSRF-TOKEN": csrf_token}
    # Solo verificamos que no devuelva 403 (devolverá 422 o 200)
    response = client.post("/clinic-scheduling-api/v1/appointments/", json={}, headers=headers)
    assert response.status_code != 403
