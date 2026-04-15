import pytest
from datetime import datetime, timedelta
from models import AppointmentStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def future_slot(days_ahead=1, duration_minutes=30):
    start = datetime.now() + timedelta(days=days_ahead)
    end = start + timedelta(minutes=duration_minutes)
    return start.isoformat(), end.isoformat()


def create_appointment(client, dentist_user_id=1, days_ahead=1):
    start, end = future_slot(days_ahead)
    res = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": dentist_user_id, "start_time_utc": start, "end_time_utc": end, "reason": "Test"}
    )
    assert res.status_code == 200, f"create failed: {res.json()}"
    return res.json()["appointment_id"]


# ---------------------------------------------------------------------------
# Tests de listado
# ---------------------------------------------------------------------------

def test_list_appointments_admin_sees_all(client):
    """ADMIN ve todos los turnos de la clínica."""
    create_appointment(client, dentist_user_id=1, days_ahead=1)
    create_appointment(client, dentist_user_id=2, days_ahead=2)
    res = client.get("/clinic-scheduling-api/v1/appointments/")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    assert len(data["appointments"]) == 2


def test_list_appointments_dentist_sees_own(client, other_dentist_client):
    """Dentista solo ve sus propios turnos."""
    # client es user_id=1 DENTIST+ADMIN, crea un turno para dentist 1
    create_appointment(client, dentist_user_id=1, days_ahead=1)
    # other_dentist es user_id=99 DENTIST, no debería ver ese turno
    res = other_dentist_client.get("/clinic-scheduling-api/v1/appointments/")
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_list_appointments_filter_by_status(client):
    """Filtrar por status devuelve solo los turnos en ese estado."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=1)
    create_appointment(client, dentist_user_id=1, days_ahead=2)
    # Aprobar uno
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status", json={"status": "APPROVED"})
    res = client.get("/clinic-scheduling-api/v1/appointments/?status=APPROVED")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["appointments"][0]["status"] == "APPROVED"


def test_list_appointments_patient_sees_own(patient_client):
    """PATIENT solo ve sus propios turnos."""
    start, end = future_slot(days_ahead=3)
    patient_client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "start_time_utc": start, "end_time_utc": end}
    )
    res = patient_client.get("/clinic-scheduling-api/v1/appointments/")
    assert res.status_code == 200
    assert res.json()["total"] == 1


# ---------------------------------------------------------------------------
# Tests originales
# ---------------------------------------------------------------------------

def test_request_appointment_success(client):
    """Cualquier usuario autenticado puede solicitar un turno."""
    start, end = future_slot()
    response = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 2, "start_time_utc": start, "end_time_utc": end, "reason": "Limpieza dental"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "REQUESTED"
    assert data["reason"] == "Limpieza dental"
    assert data["patient_user_id"] == 1  # user_id del mock de auth


def test_approve_appointment_status(client):
    """El dentista asignado puede aprobar su propio turno."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=2)
    response = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "APPROVED", "change_reason": "Espacio disponible"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"


def test_get_availability_unauthorized(client):
    """Sin configuración de Google Calendar devuelve 400."""
    response = client.get(
        "/clinic-scheduling-api/v1/appointments/availability/dentist/99"
        "?start=2027-01-01T10:00:00&end=2027-01-01T11:00:00"
    )
    assert response.status_code == 400
    assert "not connected Google Calendar" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Tests de RBAC — aprobación/rechazo/completado
# ---------------------------------------------------------------------------

def test_patient_cannot_approve_appointment(client, patient_client):
    """Un PATIENT no puede aprobar turnos."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=3)
    response = patient_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "APPROVED"}
    )
    assert response.status_code == 403


def test_patient_cannot_reject_appointment(client, patient_client):
    """Un PATIENT no puede rechazar turnos."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=3)
    response = patient_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "REJECTED"}
    )
    assert response.status_code == 403


def test_other_dentist_cannot_approve_appointment(client, other_dentist_client):
    """Un dentista NO asignado al turno no puede aprobarlo."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=4)
    response = other_dentist_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "APPROVED"}
    )
    assert response.status_code == 403


def test_assigned_dentist_can_reject(client):
    """El dentista asignado puede rechazar su turno."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=5)
    response = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "REJECTED", "change_reason": "Conflicto de agenda"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"


def test_admin_can_approve_any_appointment(client):
    """ADMIN puede aprobar cualquier turno (mock tiene roles DENTIST+ADMIN)."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=6)
    response = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "APPROVED"}
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Tests de RBAC — cancelación
# ---------------------------------------------------------------------------

def test_patient_can_cancel_own_appointment(patient_client):
    """Un PATIENT puede cancelar su propio turno (patient_user_id=10 en el mock)."""
    start, end = future_slot(days_ahead=7)
    create_res = patient_client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "start_time_utc": start, "end_time_utc": end}
    )
    assert create_res.status_code == 200
    apt_id = create_res.json()["appointment_id"]

    response = patient_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "CANCELLED", "change_reason": "No puedo asistir"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_patient_cannot_cancel_another_patients_appointment(patient_client):
    """Un PATIENT no puede cancelar el turno de otro paciente.

    El appointment se crea directamente en DB con patient_user_id=1,
    evitando el conflicto de dependency_overrides cuando dos fixtures
    modifican la misma clave de app.dependency_overrides simultáneamente.
    """
    from conftest import TestingSessionLocal
    from models import Appointment, AppointmentStatus

    db = TestingSessionLocal()
    start = datetime.now() + timedelta(days=8)
    end = start + timedelta(minutes=30)
    apt = Appointment(
        clinic_id=1,
        patient_user_id=1,   # pertenece al usuario 1, NO al patient_client (user_id=10)
        dentist_user_id=1,
        start_time_utc=start,
        end_time_utc=end,
        status=AppointmentStatus.REQUESTED
    )
    db.add(apt)
    db.commit()
    apt_id = apt.appointment_id
    db.close()

    response = patient_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "CANCELLED"}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tests de máquina de estados
# ---------------------------------------------------------------------------

def test_cannot_approve_completed_appointment(client):
    """No se puede aprobar un turno ya COMPLETED."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=9)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status", json={"status": "APPROVED"})
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status", json={"status": "COMPLETED"})

    response = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "APPROVED"}
    )
    assert response.status_code == 422

def test_cannot_transition_from_cancelled(client):
    """No se puede cambiar el estado de un turno CANCELLED."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=10)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status", json={"status": "CANCELLED"})

    response = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "APPROVED"}
    )
    assert response.status_code == 422

def test_cannot_transition_from_rejected(client):
    """No se puede cambiar el estado de un turno REJECTED."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=11)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status", json={"status": "REJECTED"})

    response = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "APPROVED"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Tests de validación Pydantic
# ---------------------------------------------------------------------------

def test_appointment_start_in_past_rejected(client):
    """start en el pasado debe devolver 422."""
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    future = (datetime.now() + timedelta(hours=2)).isoformat()
    response = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "start_time_utc": past, "end_time_utc": future}
    )
    assert response.status_code == 422


def test_appointment_end_before_start_rejected(client):
    """end <= start debe devolver 422."""
    start = (datetime.now() + timedelta(days=1)).isoformat()
    end = (datetime.now() + timedelta(hours=1)).isoformat()  # antes que start
    response = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "start_time_utc": start, "end_time_utc": end}
    )
    assert response.status_code == 422


def test_appointment_duration_too_short_rejected(client):
    """Duración menor a 15 minutos debe devolver 422."""
    start = datetime.now() + timedelta(days=1)
    end = start + timedelta(minutes=5)
    response = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "start_time_utc": start.isoformat(), "end_time_utc": end.isoformat()}
    )
    assert response.status_code == 422
