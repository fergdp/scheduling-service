import pytest
from datetime import datetime, timedelta
from models import Appointment, AppointmentStatus

def test_request_appointment_success(client):
    """Verifica que un paciente pueda solicitar un turno."""
    start = datetime.now() + timedelta(days=1)
    end = start + timedelta(minutes=30)
    
    payload = {
        "dentist_user_id": 2,
        "start_time_utc": start.isoformat(),
        "end_time_utc": end.isoformat(),
        "reason": "Limpieza dental"
    }
    
    response = client.post("/clinic-scheduling-api/v1/appointments/", json=payload)
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "REQUESTED"
    assert data["reason"] == "Limpieza dental"
    assert data["patient_user_id"] == 1 # Del mock de auth

def test_approve_appointment_status(client):
    """Verifica que el dentista pueda cambiar el estado a APPROVED."""
    # 1. Crear cita previa
    start = datetime.now() + timedelta(days=2)
    apt = {
        "dentist_user_id": 1,
        "start_time_utc": start.isoformat(),
        "end_time_utc": (start + timedelta(minutes=30)).isoformat(),
        "reason": "Ortodoncia"
    }
    create_res = client.post("/clinic-scheduling-api/v1/appointments/", json=apt)
    apt_id = create_res.json()["appointment_id"]

    # 2. Aprobar cita
    update_payload = {
        "status": "APPROVED",
        "change_reason": "Espacio disponible confirmado"
    }
    response = client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status", json=update_payload)
    
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"

def test_get_availability_unauthorized(client):
    """Verifica que si no hay configuración de Google, falle con 400."""
    # Como no hemos creado un DentistCalendarConfig en SQLite para el dentista 99, debe fallar.
    response = client.get("/clinic-scheduling-api/v1/appointments/availability/dentist/99?start=2024-01-01T10:00:00&end=2024-01-01T11:00:00")
    assert response.status_code == 400
    assert "not connected Google Calendar" in response.json()["detail"]
