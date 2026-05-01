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


def create_appointment(client, dentist_user_id=1, patient_user_id=5, days_ahead=1, reason="Test"):
    """Crea un turno via API. El client debe tener rol ADMIN o RECEPTIONIST."""
    start, end = future_slot(days_ahead)
    res = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={
            "dentist_user_id": dentist_user_id,
            "patient_user_id": patient_user_id,
            "patient_name": "Test Patient",
            "start_time_utc": start,
            "end_time_utc": end,
            "reason": reason,
        }
    )
    assert res.status_code == 200, f"create failed: {res.json()}"
    return res.json()["appointment_id"]


def insert_appointment_db(dentist_user_id=1, patient_user_id=5, days_ahead=1,
                           status=AppointmentStatus.SCHEDULED):
    """Inserta un turno directo en DB, sin pasar por la API ni dependency_overrides."""
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    start = datetime.now() + timedelta(days=days_ahead)
    end = start + timedelta(minutes=30)
    apt = Appointment(
        clinic_id=1,
        patient_user_id=patient_user_id,
        dentist_user_id=dentist_user_id,
        start_time_utc=start,
        end_time_utc=end,
        status=status,
    )
    db.add(apt)
    db.commit()
    apt_id = apt.appointment_id
    db.close()
    return apt_id


# ---------------------------------------------------------------------------
# Importación tardía (evita ciclo en módulo)
# ---------------------------------------------------------------------------

from models import Appointment


# ---------------------------------------------------------------------------
# Tests de creación
# ---------------------------------------------------------------------------

def test_create_appointment_success(client):
    """ADMIN puede crear un turno confirmado (SCHEDULED)."""
    start, end = future_slot()
    res = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={
            "dentist_user_id": 1,
            "patient_user_id": 5,
            "patient_name": "María García",
            "patient_email": "maria@example.com",
            "start_time_utc": start,
            "end_time_utc": end,
            "reason": "Limpieza dental",
        }
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SCHEDULED"
    assert data["patient_user_id"] == 5
    assert data["patient_name"] == "María García"
    assert data["dentist_user_id"] == 1


def test_create_appointment_without_end_uses_default_duration(client):
    """Si no se envía end_time_utc, se calcula con la duración default (30 min)."""
    start = (datetime.now() + timedelta(days=1)).isoformat()
    res = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "patient_user_id": 5, "start_time_utc": start}
    )
    assert res.status_code == 200
    data = res.json()
    start_dt = datetime.fromisoformat(data["start_time_utc"].replace("Z", "+00:00"))
    end_dt   = datetime.fromisoformat(data["end_time_utc"].replace("Z", "+00:00"))
    assert (end_dt - start_dt).total_seconds() == 1800  # 30 min


def test_patient_cannot_create_appointment(patient_client):
    """PATIENT no puede crear turnos (solo ADMIN/RECEPTIONIST)."""
    start, end = future_slot()
    res = patient_client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "patient_user_id": 10, "start_time_utc": start, "end_time_utc": end}
    )
    assert res.status_code == 403


def test_dentist_can_create_appointment(other_dentist_client):
    """DENTIST puede crear turnos (no todos tienen recepcionista)."""
    start, end = future_slot()
    res = other_dentist_client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 99, "patient_user_id": 5, "start_time_utc": start, "end_time_utc": end}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "SCHEDULED"


def test_create_appointment_past_start_rejected(client):
    """start en el pasado devuelve 422."""
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    future = (datetime.now() + timedelta(hours=2)).isoformat()
    res = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "patient_user_id": 5, "start_time_utc": past, "end_time_utc": future}
    )
    assert res.status_code == 422


def test_create_appointment_duration_too_short(client):
    """Duración menor a 15 min devuelve 422."""
    start = datetime.now() + timedelta(days=1)
    end = start + timedelta(minutes=5)
    res = client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={"dentist_user_id": 1, "patient_user_id": 5,
              "start_time_utc": start.isoformat(), "end_time_utc": end.isoformat()}
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Tests de solapamiento
# ---------------------------------------------------------------------------

def test_overlap_same_slot_returns_409(client):
    """Dos turnos para el mismo dentista y horario devuelve 409 en el segundo."""
    start, end = future_slot(days_ahead=20)
    payload = {"dentist_user_id": 1, "patient_user_id": 5, "start_time_utc": start, "end_time_utc": end}
    assert client.post("/clinic-scheduling-api/v1/appointments/", json=payload).status_code == 200
    assert client.post("/clinic-scheduling-api/v1/appointments/", json=payload).status_code == 409


def test_overlap_partial_returns_409(client):
    """Solapamiento parcial (10:00-11:00 vs 10:30-11:30) devuelve 409."""
    base = (datetime.now() + timedelta(days=21)).replace(hour=10, minute=0, second=0, microsecond=0)
    first = client.post("/clinic-scheduling-api/v1/appointments/", json={
        "dentist_user_id": 1, "patient_user_id": 5,
        "start_time_utc": base.isoformat(),
        "end_time_utc": (base + timedelta(hours=1)).isoformat()
    })
    assert first.status_code == 200
    second = client.post("/clinic-scheduling-api/v1/appointments/", json={
        "dentist_user_id": 1, "patient_user_id": 6,
        "start_time_utc": (base + timedelta(minutes=30)).isoformat(),
        "end_time_utc": (base + timedelta(hours=1, minutes=30)).isoformat()
    })
    assert second.status_code == 409


def test_no_overlap_different_dentist(client):
    """Mismo horario para distinto dentista no genera conflicto."""
    start, end = future_slot(days_ahead=22)
    r1 = client.post("/clinic-scheduling-api/v1/appointments/",
                     json={"dentist_user_id": 1, "patient_user_id": 5, "start_time_utc": start, "end_time_utc": end})
    r2 = client.post("/clinic-scheduling-api/v1/appointments/",
                     json={"dentist_user_id": 2, "patient_user_id": 5, "start_time_utc": start, "end_time_utc": end})
    assert r1.status_code == 200
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Tests de listado
# ---------------------------------------------------------------------------

def test_list_appointments_admin_sees_all(client):
    """ADMIN ve todos los turnos de la clínica."""
    create_appointment(client, dentist_user_id=1, days_ahead=1)
    create_appointment(client, dentist_user_id=2, days_ahead=2)
    res = client.get("/clinic-scheduling-api/v1/appointments/")
    assert res.status_code == 200
    assert res.json()["total"] == 2


def test_list_appointments_dentist_sees_own(other_dentist_client):
    """DENTIST solo ve los turnos asignados a él."""
    insert_appointment_db(dentist_user_id=1, days_ahead=3)  # turno de otro dentista
    res = other_dentist_client.get("/clinic-scheduling-api/v1/appointments/")
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_list_appointments_filter_by_status(client):
    """Filtrar por status devuelve solo los coincidentes."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=4)
    create_appointment(client, dentist_user_id=1, days_ahead=5)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
                 json={"status": "CANCELLED"})
    res = client.get("/clinic-scheduling-api/v1/appointments/?status=CANCELLED")
    assert res.status_code == 200
    assert res.json()["total"] == 1


def test_list_appointments_patient_sees_own(patient_client):
    """PATIENT solo ve sus propios turnos."""
    # El patient_client (user_id=10) no tiene rol para crear, así que insertamos directo
    insert_appointment_db(patient_user_id=10, days_ahead=6)
    insert_appointment_db(patient_user_id=5, days_ahead=7)  # de otro paciente
    res = patient_client.get("/clinic-scheduling-api/v1/appointments/")
    assert res.status_code == 200
    assert res.json()["total"] == 1


# ---------------------------------------------------------------------------
# Tests de detalle
# ---------------------------------------------------------------------------

def test_get_appointment_detail_success(client):
    """ADMIN puede obtener el detalle de cualquier turno."""
    apt_id = create_appointment(client, days_ahead=8)
    res = client.get(f"/clinic-scheduling-api/v1/appointments/{apt_id}")
    assert res.status_code == 200
    assert res.json()["appointment_id"] == apt_id


def test_get_appointment_detail_not_found(client):
    """ID inexistente devuelve 404."""
    res = client.get("/clinic-scheduling-api/v1/appointments/999999")
    assert res.status_code == 404


def test_get_appointment_detail_patient_own(patient_client):
    """PATIENT puede ver su propio turno."""
    apt_id = insert_appointment_db(patient_user_id=10, days_ahead=9)
    res = patient_client.get(f"/clinic-scheduling-api/v1/appointments/{apt_id}")
    assert res.status_code == 200


def test_get_appointment_detail_patient_others_forbidden(patient_client):
    """PATIENT no puede ver el turno de otro paciente."""
    apt_id = insert_appointment_db(patient_user_id=1, days_ahead=10)
    res = patient_client.get(f"/clinic-scheduling-api/v1/appointments/{apt_id}")
    assert res.status_code == 403


def test_get_appointment_detail_dentist_own(client):
    """DENTIST puede ver sus propios turnos."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=11)
    res = client.get(f"/clinic-scheduling-api/v1/appointments/{apt_id}")
    assert res.status_code == 200


def test_get_appointment_detail_other_dentist_forbidden(other_dentist_client):
    """DENTIST no puede ver turnos de otro dentista."""
    apt_id = insert_appointment_db(dentist_user_id=1, days_ahead=12)
    res = other_dentist_client.get(f"/clinic-scheduling-api/v1/appointments/{apt_id}")
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Tests de edición (PUT)
# ---------------------------------------------------------------------------

def test_update_appointment_success(client):
    """ADMIN puede editar fecha y motivo de un turno."""
    apt_id = create_appointment(client, days_ahead=13)
    new_start = (datetime.now() + timedelta(days=14)).isoformat()
    new_end   = (datetime.now() + timedelta(days=14, hours=1)).isoformat()
    res = client.put(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}",
        json={"start_time_utc": new_start, "end_time_utc": new_end, "reason": "Ortodoncia"}
    )
    assert res.status_code == 200
    assert res.json()["reason"] == "Ortodoncia"


def test_update_appointment_overlap_rejected(client):
    """Editar un turno a un horario ocupado devuelve 409."""
    apt1_id = create_appointment(client, dentist_user_id=1, days_ahead=15)
    apt2_id = create_appointment(client, dentist_user_id=1, days_ahead=16)

    # Intentar mover apt2 al mismo slot que apt1
    start1 = datetime.now() + timedelta(days=15)
    end1   = start1 + timedelta(minutes=30)
    res = client.put(
        f"/clinic-scheduling-api/v1/appointments/{apt2_id}",
        json={"start_time_utc": start1.isoformat(), "end_time_utc": end1.isoformat()}
    )
    assert res.status_code == 409


def test_update_completed_appointment_rejected(client):
    """No se puede editar un turno que ya está COMPLETED."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=17)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
                 json={"status": "COMPLETED"})
    new_start = (datetime.now() + timedelta(days=18)).isoformat()
    res = client.put(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}",
        json={"start_time_utc": new_start}
    )
    assert res.status_code == 422


def test_dentist_cannot_edit_others_appointment(other_dentist_client):
    """DENTIST no puede editar el turno de otro dentista."""
    apt_id = insert_appointment_db(dentist_user_id=1, days_ahead=19)
    new_start = (datetime.now() + timedelta(days=20)).isoformat()
    res = other_dentist_client.put(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}",
        json={"start_time_utc": new_start}
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Tests de cambio de estado (PATCH /status)
# ---------------------------------------------------------------------------

def test_complete_appointment_by_dentist(client):
    """Odontólogo asignado puede marcar un turno como COMPLETED."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=23)
    res = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "COMPLETED", "change_reason": "Consulta finalizada"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "COMPLETED"


def test_cancel_appointment_by_patient(patient_client):
    """PATIENT puede cancelar su propio turno."""
    apt_id = insert_appointment_db(patient_user_id=10, days_ahead=24)
    res = patient_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "CANCELLED", "change_reason": "No puedo asistir"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"


def test_patient_cannot_complete_appointment(patient_client):
    """PATIENT no puede marcar un turno como COMPLETED."""
    apt_id = insert_appointment_db(patient_user_id=10, days_ahead=25)
    res = patient_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "COMPLETED"}
    )
    assert res.status_code == 403


def test_other_dentist_cannot_complete_appointment(other_dentist_client):
    """Dentista no asignado no puede completar el turno."""
    apt_id = insert_appointment_db(dentist_user_id=1, days_ahead=26)
    res = other_dentist_client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "COMPLETED"}
    )
    assert res.status_code == 403


def test_cannot_change_status_of_cancelled_appointment(client):
    """No se puede cambiar el estado de un turno ya CANCELLED."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=27)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
                 json={"status": "CANCELLED"})
    res = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "COMPLETED"}
    )
    assert res.status_code == 422


def test_cannot_change_status_of_completed_appointment(client):
    """No se puede cambiar el estado de un turno ya COMPLETED."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=28)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
                 json={"status": "COMPLETED"})
    res = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "CANCELLED"}
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Tests de upcoming (dashboard)
# ---------------------------------------------------------------------------

def test_upcoming_returns_future_scheduled(client):
    """GET /upcoming devuelve solo turnos SCHEDULED futuros, ordenados por fecha ASC."""
    create_appointment(client, dentist_user_id=1, days_ahead=1)
    create_appointment(client, dentist_user_id=1, days_ahead=2)
    res = client.get("/clinic-scheduling-api/v1/appointments/upcoming?limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    # Deben estar ordenados ASC
    starts = [a["start_time_utc"] for a in data["appointments"]]
    assert starts == sorted(starts)


def test_upcoming_respects_limit(client):
    """El parámetro limit es respetado."""
    for i in range(5):
        create_appointment(client, dentist_user_id=1, days_ahead=i + 1)
    res = client.get("/clinic-scheduling-api/v1/appointments/upcoming?limit=3")
    assert res.status_code == 200
    assert len(res.json()["appointments"]) == 3


def test_upcoming_does_not_include_cancelled(client):
    """Turnos cancelados no aparecen en upcoming."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=1)
    client.patch(f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
                 json={"status": "CANCELLED"})
    res = client.get("/clinic-scheduling-api/v1/appointments/upcoming")
    assert res.status_code == 200
    assert res.json()["total"] == 0


# ---------------------------------------------------------------------------
# Tests de disponibilidad (GET /availability)
# ---------------------------------------------------------------------------

def test_get_availability_no_gcal_connected(client):
    """Sin Google Calendar conectado devuelve 400."""
    res = client.get(
        "/clinic-scheduling-api/v1/appointments/availability/dentist/99"
        "?start=2027-01-01T10:00:00&end=2027-01-01T11:00:00"
    )
    assert res.status_code == 400
    assert "not connected Google Calendar" in res.json()["detail"]
