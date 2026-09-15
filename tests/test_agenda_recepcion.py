"""
Agenda de recepción (epic #294): seis estados sin transiciones terminales (#285), la
recepcionista administra los turnos de todos los profesionales (#286) y el borrado lógico
distinto de cancelar (#287).

Los helpers de creación viven en test_appointments.py; acá se reusan.
"""
import pytest
from datetime import datetime, timedelta

from models import Appointment, AppointmentAuditLog, AppointmentStatus, DentistCalendarConfig
from test_appointments import create_appointment, future_slot

BASE = "/clinic-scheduling-api/v1/appointments"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db():
    from conftest import TestingSessionLocal
    return TestingSessionLocal()


def _insert(dentist_user_id=1, patient_user_id=5, days_ahead=1, clinic_id=1,
            status=AppointmentStatus.SCHEDULED, google_event_id=None, patient_phone=None):
    """Turno directo en DB, con clínica, estado y evento de Google a elección."""
    db = _db()
    start = datetime.now() + timedelta(days=days_ahead)
    apt = Appointment(
        clinic_id=clinic_id,
        patient_user_id=patient_user_id,
        dentist_user_id=dentist_user_id,
        patient_name="Paciente Test",
        patient_phone=patient_phone,
        start_time_utc=start,
        end_time_utc=start + timedelta(minutes=30),
        status=status,
        google_event_id=google_event_id,
    )
    db.add(apt)
    db.commit()
    apt_id = apt.appointment_id
    db.close()
    return apt_id


def _estado(apt_id):
    db = _db()
    try:
        return db.get(Appointment, apt_id).status
    finally:
        db.close()


def _fila(apt_id):
    db = _db()
    try:
        apt = db.get(Appointment, apt_id)
        db.refresh(apt)
        return {c.name: getattr(apt, c.name) for c in Appointment.__table__.columns}
    finally:
        db.close()


def _auditoria(apt_id):
    db = _db()
    try:
        return [
            (a.previous_status, a.new_status, a.change_reason)
            for a in db.query(AppointmentAuditLog)
                       .filter(AppointmentAuditLog.appointment_id == apt_id)
                       .order_by(AppointmentAuditLog.log_id).all()
        ]
    finally:
        db.close()


def _patch(cliente, apt_id, status):
    return cliente.patch(f"{BASE}/{apt_id}/status", json={"status": status})


def _con_google(monkeypatch, dentist_ids):
    """
    Deja a los odontólogos dados con Google Calendar "conectado" y reemplaza las llamadas
    a Google por mocks que anotan qué se hizo. Devuelve la lista de llamadas.
    """
    db = _db()
    for d in dentist_ids:
        db.add(DentistCalendarConfig(
            dentist_user_id=d, clinic_id=1, google_email=f"dentista{d}@gmail.com",
            google_access_token="acceso", google_refresh_token="refresh",
        ))
    db.commit()
    db.close()

    llamadas = []
    import routers.appointments as ra
    monkeypatch.setattr(ra, "decrypt_token", lambda t: t)
    monkeypatch.setattr(ra, "get_calendar_service", lambda a, r, e: "servicio")

    def _create(service, cal, start, end, summary, description, attendees):
        llamadas.append(("create", attendees))
        return f"evt-{len(llamadas)}"

    monkeypatch.setattr(ra, "create_google_event", _create)
    monkeypatch.setattr(ra, "update_google_event",
                        lambda *a, **k: llamadas.append(("update",)))
    monkeypatch.setattr(ra, "delete_google_event",
                        lambda service, cal, event_id: llamadas.append(("delete", event_id)))
    return llamadas


# ---------------------------------------------------------------------------
# #285 — Seis estados, transiciones libres para el staff
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nuevo", ["CONFIRMED", "ARRIVED", "COMPLETED", "CANCELLED", "NO_SHOW"])
def test_receptionist_moves_scheduled_to_any_status(receptionist_client, nuevo):
    """La recepcionista pasa un turno programado a cualquiera de los otros cinco estados."""
    apt_id = _insert(days_ahead=2)
    res = _patch(receptionist_client, apt_id, nuevo)
    assert res.status_code == 200, res.json()
    assert res.json()["status"] == nuevo
    assert _estado(apt_id) == AppointmentStatus[nuevo]


def test_full_front_desk_flow_and_back(receptionist_client):
    """Programado → Confirmado → En espera → Atendido → (error) → Programado. Nada es terminal."""
    apt_id = _insert(days_ahead=3)
    for estado in ("CONFIRMED", "ARRIVED", "COMPLETED", "SCHEDULED"):
        res = _patch(receptionist_client, apt_id, estado)
        assert res.status_code == 200, (estado, res.json())
    assert _estado(apt_id) == AppointmentStatus.SCHEDULED
    assert [a[:2] for a in _auditoria(apt_id)] == [
        ("SCHEDULED", "CONFIRMED"), ("CONFIRMED", "ARRIVED"),
        ("ARRIVED", "COMPLETED"), ("COMPLETED", "SCHEDULED"),
    ]


def test_same_status_is_rejected(receptionist_client):
    """Marcar el estado que ya tiene no es un cambio: 422 y sin fila de auditoría."""
    apt_id = _insert(days_ahead=4, status=AppointmentStatus.CONFIRMED)
    res = _patch(receptionist_client, apt_id, "CONFIRMED")
    assert res.status_code == 422
    assert _auditoria(apt_id) == []


def test_no_show_can_become_arrived(receptionist_client):
    """El ausente que llegó tarde vuelve a la sala de espera."""
    apt_id = _insert(days_ahead=5, status=AppointmentStatus.NO_SHOW)
    res = _patch(receptionist_client, apt_id, "ARRIVED")
    assert res.status_code == 200
    assert res.json()["status"] == "ARRIVED"


def test_reactivating_cancelled_on_taken_slot_is_409(receptionist_client):
    """
    Mientras estuvo cancelado, el hueco se ocupó con otro turno: volver a programarlo
    se rechaza con 409 y el turno sigue cancelado.
    """
    original = _insert(days_ahead=6, status=AppointmentStatus.CANCELLED)
    fila = _fila(original)
    db = _db()
    db.add(Appointment(
        clinic_id=1, dentist_user_id=1, patient_user_id=8,
        start_time_utc=fila["start_time_utc"], end_time_utc=fila["end_time_utc"],
        status=AppointmentStatus.SCHEDULED,
    ))
    db.commit()
    db.close()

    res = _patch(receptionist_client, original, "SCHEDULED")
    assert res.status_code == 409
    assert _estado(original) == AppointmentStatus.CANCELLED


def test_reactivating_cancelled_on_free_slot_works(receptionist_client):
    apt_id = _insert(days_ahead=7, status=AppointmentStatus.CANCELLED)
    res = _patch(receptionist_client, apt_id, "SCHEDULED")
    assert res.status_code == 200
    assert res.json()["status"] == "SCHEDULED"


def test_arrived_to_completed_does_not_check_overlap(receptionist_client):
    """Entre dos estados activos no hay re-chequeo: el turno ya tenía el hueco."""
    apt_id = _insert(days_ahead=8, status=AppointmentStatus.ARRIVED)
    res = _patch(receptionist_client, apt_id, "COMPLETED")
    assert res.status_code == 200


@pytest.mark.parametrize("ocupa", ["CONFIRMED", "ARRIVED"])
def test_confirmed_and_arrived_block_the_slot(client, ocupa):
    """Un turno confirmado o en espera ocupa el hueco igual que uno programado."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=9)
    assert _patch(client, apt_id, ocupa).status_code == 200
    start, end = future_slot(days_ahead=9)
    res = client.post(f"{BASE}/", json={
        "dentist_user_id": 1, "patient_user_id": 6,
        "start_time_utc": start, "end_time_utc": end,
    })
    assert res.status_code == 409


@pytest.mark.parametrize("libera", ["NO_SHOW", "COMPLETED", "CANCELLED"])
def test_inactive_statuses_free_the_slot(client, libera):
    """Ausente, atendido y cancelado liberan el hueco para otro turno."""
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=10)
    assert _patch(client, apt_id, libera).status_code == 200
    start, end = future_slot(days_ahead=10)
    res = client.post(f"{BASE}/", json={
        "dentist_user_id": 1, "patient_user_id": 6,
        "start_time_utc": start, "end_time_utc": end,
    })
    assert res.status_code == 200, res.json()


def test_patient_cannot_confirm_own_appointment(patient_client):
    """El paciente no confirma ni marca nada: sólo cancela."""
    apt_id = _insert(patient_user_id=10, days_ahead=11)
    res = _patch(patient_client, apt_id, "CONFIRMED")
    assert res.status_code == 403
    assert _estado(apt_id) == AppointmentStatus.SCHEDULED


def test_patient_can_cancel_confirmed_appointment(patient_client):
    apt_id = _insert(patient_user_id=10, days_ahead=12, status=AppointmentStatus.CONFIRMED)
    res = _patch(patient_client, apt_id, "CANCELLED")
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"


def test_patient_cannot_cancel_once_arrived(patient_client):
    """Ya está en la sala de espera: cancelar por el portal no corresponde."""
    apt_id = _insert(patient_user_id=10, days_ahead=13, status=AppointmentStatus.ARRIVED)
    res = _patch(patient_client, apt_id, "CANCELLED")
    assert res.status_code == 403
    assert _estado(apt_id) == AppointmentStatus.ARRIVED


def test_patient_cannot_reactivate_own_cancelled(patient_client):
    apt_id = _insert(patient_user_id=10, days_ahead=14, status=AppointmentStatus.CANCELLED)
    assert _patch(patient_client, apt_id, "SCHEDULED").status_code == 403


def test_other_dentist_cannot_change_colleague_status(other_dentist_client):
    """Un odontólogo no administra los turnos de un colega, tampoco con los estados nuevos."""
    apt_id = _insert(dentist_user_id=1, days_ahead=15)
    assert _patch(other_dentist_client, apt_id, "ARRIVED").status_code == 403


def test_upcoming_includes_confirmed_and_arrived_but_not_no_show(client):
    """El widget del dashboard muestra lo que ocupa el hueco: los tres estados activos."""
    _insert(days_ahead=1, status=AppointmentStatus.CONFIRMED)
    _insert(days_ahead=2, status=AppointmentStatus.ARRIVED)
    _insert(days_ahead=3, status=AppointmentStatus.NO_SHOW)
    _insert(days_ahead=4, status=AppointmentStatus.COMPLETED)
    res = client.get(f"{BASE}/upcoming?limit=10")
    assert res.status_code == 200
    assert sorted(a["status"] for a in res.json()["appointments"]) == ["ARRIVED", "CONFIRMED"]


def test_cancel_deletes_google_event_and_reactivate_recreates_it(receptionist_client, monkeypatch):
    """Cancelar saca el evento del Google del odontólogo; volver a programar lo crea de nuevo."""
    llamadas = _con_google(monkeypatch, [1])
    apt_id = _insert(days_ahead=16, google_event_id="evt-viejo")

    assert _patch(receptionist_client, apt_id, "CANCELLED").status_code == 200
    assert llamadas == [("delete", "evt-viejo")]
    assert _fila(apt_id)["google_event_id"] is None

    assert _patch(receptionist_client, apt_id, "SCHEDULED").status_code == 200
    assert llamadas[-1][0] == "create"
    assert _fila(apt_id)["google_event_id"] == "evt-2"


@pytest.mark.parametrize("nuevo", ["CONFIRMED", "ARRIVED", "COMPLETED", "NO_SHOW"])
def test_statuses_other_than_cancelled_do_not_touch_google(receptionist_client, monkeypatch, nuevo):
    llamadas = _con_google(monkeypatch, [1])
    apt_id = _insert(days_ahead=17, google_event_id="evt-quieto")
    assert _patch(receptionist_client, apt_id, nuevo).status_code == 200
    assert llamadas == []
    assert _fila(apt_id)["google_event_id"] == "evt-quieto"


# ---------------------------------------------------------------------------
# #286 — Recepción administra a todos los profesionales
# ---------------------------------------------------------------------------

def test_receptionist_can_read_appointment_detail(receptionist_client):
    """Antes: 403. La agenda de toda la clínica es su trabajo."""
    apt_id = _insert(dentist_user_id=1, days_ahead=18, patient_phone="351-555-0000")
    res = receptionist_client.get(f"{BASE}/{apt_id}")
    assert res.status_code == 200
    assert res.json()["appointment_id"] == apt_id
    assert res.json()["patient_phone"] == "351-555-0000"


def test_receptionist_of_other_clinic_gets_404(other_clinic_client):
    """Multi-tenant: un turno de otra clínica no existe para ella, ni para leer ni para tocar."""
    apt_id = _insert(clinic_id=1, days_ahead=19)
    assert other_clinic_client.get(f"{BASE}/{apt_id}").status_code == 404
    assert _patch(other_clinic_client, apt_id, "COMPLETED").status_code == 404
    assert other_clinic_client.delete(f"{BASE}/{apt_id}").status_code == 404
    assert _estado(apt_id) == AppointmentStatus.SCHEDULED


def test_receptionist_can_mark_completed(receptionist_client):
    """Antes: 403 ("only the assigned dentist or ADMIN"). El E2E SR2 fijaba ese 403."""
    apt_id = _insert(dentist_user_id=1, days_ahead=20)
    res = _patch(receptionist_client, apt_id, "COMPLETED")
    assert res.status_code == 200
    assert res.json()["status"] == "COMPLETED"


def test_receptionist_can_edit_confirmed_appointment(receptionist_client):
    """Editar ya no exige SCHEDULED: cualquier estado activo se puede reprogramar."""
    apt_id = _insert(days_ahead=21, status=AppointmentStatus.CONFIRMED)
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"reason": "Control"})
    assert res.status_code == 200
    assert res.json()["reason"] == "Control"
    assert res.json()["status"] == "CONFIRMED"


@pytest.mark.parametrize("inactivo", [AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW,
                                      AppointmentStatus.CANCELLED])
def test_inactive_appointment_cannot_be_edited(receptionist_client, inactivo):
    apt_id = _insert(days_ahead=22, status=inactivo)
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"reason": "x"})
    assert res.status_code == 422


def test_receptionist_reassigns_dentist(receptionist_client):
    """Cambiar de profesional: el turno pasa al odontólogo nuevo y queda en la auditoría."""
    apt_id = _insert(dentist_user_id=1, days_ahead=23)
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2})
    assert res.status_code == 200
    assert res.json()["dentist_user_id"] == 2
    assert _fila(apt_id)["dentist_user_id"] == 2
    assert _auditoria(apt_id) == [("SCHEDULED", "SCHEDULED", "Profesional cambiado: 1 → 2")]


def test_reassign_checks_overlap_against_new_dentist(receptionist_client):
    """El odontólogo nuevo ya tiene un turno a esa hora: 409 y no se mueve nada."""
    apt_id = _insert(dentist_user_id=1, days_ahead=24)
    fila = _fila(apt_id)
    db = _db()
    db.add(Appointment(
        clinic_id=1, dentist_user_id=2, patient_user_id=9,
        start_time_utc=fila["start_time_utc"], end_time_utc=fila["end_time_utc"],
        status=AppointmentStatus.CONFIRMED,
    ))
    db.commit()
    db.close()

    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2})
    assert res.status_code == 409
    assert _fila(apt_id)["dentist_user_id"] == 1
    assert _auditoria(apt_id) == []


def test_reassign_same_dentist_is_a_noop(receptionist_client):
    apt_id = _insert(dentist_user_id=1, days_ahead=25)
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 1})
    assert res.status_code == 200
    assert _auditoria(apt_id) == []


def test_dentist_cannot_reassign_colleague_appointment(other_dentist_client):
    apt_id = _insert(dentist_user_id=1, days_ahead=26)
    res = other_dentist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 99})
    assert res.status_code == 403
    assert _fila(apt_id)["dentist_user_id"] == 1


def test_reassign_moves_google_event_between_calendars(receptionist_client, monkeypatch):
    """Sale del Google del odontólogo anterior y entra al del nuevo, con invitación al paciente."""
    llamadas = _con_google(monkeypatch, [1, 2])
    apt_id = _insert(dentist_user_id=1, days_ahead=27, google_event_id="evt-del-1")
    db = _db()
    db.get(Appointment, apt_id).patient_email = "paciente@mail.com"
    db.commit()
    db.close()

    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2})
    assert res.status_code == 200
    assert llamadas == [("delete", "evt-del-1"), ("create", ["paciente@mail.com"])]
    fila = _fila(apt_id)
    assert fila["google_event_id"] == "evt-2"
    assert fila["gcal_sync_status"].value == "SYNCED"


def test_reassign_to_dentist_without_google_leaves_no_event(receptionist_client, monkeypatch):
    llamadas = _con_google(monkeypatch, [1])
    apt_id = _insert(dentist_user_id=1, days_ahead=28, google_event_id="evt-del-1")
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 3})
    assert res.status_code == 200
    assert llamadas == [("delete", "evt-del-1")]
    fila = _fila(apt_id)
    assert fila["google_event_id"] is None
    assert fila["gcal_sync_status"].value == "NOT_CONFIGURED"


def test_list_filters_by_dentist(receptionist_client):
    """La columna de un profesional: ?dentist_user_id."""
    _insert(dentist_user_id=1, days_ahead=29)
    _insert(dentist_user_id=2, days_ahead=29)
    _insert(dentist_user_id=2, days_ahead=30)
    res = receptionist_client.get(f"{BASE}/?dentist_user_id=2")
    assert res.status_code == 200
    assert res.json()["total"] == 2
    assert {a["dentist_user_id"] for a in res.json()["appointments"]} == {2}


def test_dentist_filter_does_not_widen_dentist_scope(other_dentist_client):
    """Un odontólogo que pide la columna de un colega no ve nada: el filtro se suma al scoping."""
    _insert(dentist_user_id=1, days_ahead=31)
    res = other_dentist_client.get(f"{BASE}/?dentist_user_id=1")
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_list_limit_accepts_500_and_rejects_501(receptionist_client):
    assert receptionist_client.get(f"{BASE}/?limit=500").status_code == 200
    assert receptionist_client.get(f"{BASE}/?limit=501").status_code == 422


# ---------------------------------------------------------------------------
# #287 — Borrar (borrado lógico) distinto de cancelar
# ---------------------------------------------------------------------------

def test_receptionist_deletes_appointment(receptionist_client):
    """Desaparece de todo: detalle, lista, próximos y solapamiento. La fila queda con deleted_at."""
    apt_id = _insert(dentist_user_id=1, days_ahead=32)
    res = receptionist_client.delete(f"{BASE}/{apt_id}")
    assert res.status_code == 204

    assert receptionist_client.get(f"{BASE}/{apt_id}").status_code == 404
    assert receptionist_client.get(f"{BASE}/").json()["total"] == 0
    assert receptionist_client.get(f"{BASE}/upcoming").json()["total"] == 0

    fila = _fila(apt_id)
    assert fila["deleted_at"] is not None
    assert fila["deleted_by_user_id"] == 50
    assert fila["status"] == AppointmentStatus.SCHEDULED  # borrar no es cancelar
    assert _auditoria(apt_id) == [("SCHEDULED", "DELETED", None)]


def test_deleted_appointment_frees_the_slot(client):
    apt_id = create_appointment(client, dentist_user_id=1, days_ahead=33)
    assert client.delete(f"{BASE}/{apt_id}").status_code == 204
    start, end = future_slot(days_ahead=33)
    res = client.post(f"{BASE}/", json={
        "dentist_user_id": 1, "patient_user_id": 6,
        "start_time_utc": start, "end_time_utc": end,
    })
    assert res.status_code == 200, res.json()


def test_deleted_appointment_cannot_be_touched_again(receptionist_client):
    apt_id = _insert(days_ahead=34)
    assert receptionist_client.delete(f"{BASE}/{apt_id}").status_code == 204
    assert receptionist_client.delete(f"{BASE}/{apt_id}").status_code == 404
    assert _patch(receptionist_client, apt_id, "COMPLETED").status_code == 404
    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"reason": "x"}).status_code == 404


def test_patient_cannot_delete(patient_client):
    apt_id = _insert(patient_user_id=10, days_ahead=35)
    assert patient_client.delete(f"{BASE}/{apt_id}").status_code == 403
    assert _fila(apt_id)["deleted_at"] is None


def test_other_dentist_cannot_delete(other_dentist_client):
    apt_id = _insert(dentist_user_id=1, days_ahead=36)
    assert other_dentist_client.delete(f"{BASE}/{apt_id}").status_code == 403
    assert _fila(apt_id)["deleted_at"] is None


def test_assigned_dentist_can_delete_own(other_dentist_client):
    apt_id = _insert(dentist_user_id=99, days_ahead=37)
    assert other_dentist_client.delete(f"{BASE}/{apt_id}").status_code == 204
    assert _fila(apt_id)["deleted_at"] is not None


def test_delete_removes_google_event(receptionist_client, monkeypatch):
    llamadas = _con_google(monkeypatch, [1])
    apt_id = _insert(dentist_user_id=1, days_ahead=38, google_event_id="evt-borrar")
    assert receptionist_client.delete(f"{BASE}/{apt_id}").status_code == 204
    assert llamadas == [("delete", "evt-borrar")]
    assert _fila(apt_id)["google_event_id"] is None


def test_deleted_appointment_is_invisible_in_patient_history(receptionist_client):
    """El filtro por paciente (solapa Turnos de la ficha) tampoco lo muestra."""
    apt_id = _insert(patient_user_id=5, days_ahead=39)
    vivo = _insert(patient_user_id=5, days_ahead=40)
    receptionist_client.delete(f"{BASE}/{apt_id}")
    res = receptionist_client.get(f"{BASE}/?patient_user_id=5")
    assert [a["appointment_id"] for a in res.json()["appointments"]] == [vivo]
