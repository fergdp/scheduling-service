"""
Hallazgos del review independiente del 2026-09-15 sobre la agenda de recepción (#285-#287):
autorización antes del oráculo de estado, reasignación sólo por ADMIN/RECEPTIONIST, odontólogo
de la clínica, Google después del commit, y qué queda en la base cuando Google falla.
"""
import pytest

from models import Appointment, AppointmentStatus, DentistCalendarConfig
from test_appointments import future_slot
from test_agenda_recepcion import (
    BASE, _auditoria, _con_google, _db, _estado, _fila, _insert, _patch,
)


def test_patient_cannot_cancel_someone_elses_appointment(patient_client):
    """La celda más importante de la matriz: el paciente 10 no toca el turno del paciente 5."""
    apt_id = _insert(patient_user_id=5, days_ahead=41)
    res = _patch(patient_client, apt_id, "CANCELLED")
    assert res.status_code == 403
    assert _estado(apt_id) == AppointmentStatus.SCHEDULED


def test_authorization_is_checked_before_state_oracle(patient_client, other_dentist_client):
    """
    El 403 sale ANTES que el 422 de "ya está en ese estado": si no, cualquiera de la clínica
    enumera ids y lee el estado de cada turno con seis PATCH. Vale para PATCH y para PUT.
    """
    apt_id = _insert(patient_user_id=5, dentist_user_id=1, days_ahead=42,
                     status=AppointmentStatus.ARRIVED)
    assert _patch(patient_client, apt_id, "ARRIVED").status_code == 403
    assert _patch(other_dentist_client, apt_id, "ARRIVED").status_code == 403

    cerrado = _insert(patient_user_id=5, dentist_user_id=1, days_ahead=43,
                      status=AppointmentStatus.COMPLETED)
    res = other_dentist_client.put(f"{BASE}/{cerrado}", json={"reason": "x"})
    assert res.status_code == 403


def test_dentist_cannot_hand_off_own_appointment_to_colleague(other_dentist_client):
    """Reasignar es de ADMIN o RECEPTIONIST: el Dr. 99 no empuja su turno a la agenda del Dr. 1."""
    apt_id = _insert(dentist_user_id=99, days_ahead=44)
    res = other_dentist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 1})
    assert res.status_code == 403
    assert _fila(apt_id)["dentist_user_id"] == 99
    # Su propio horario sí lo puede seguir tocando.
    res = other_dentist_client.put(f"{BASE}/{apt_id}", json={"reason": "control"})
    assert res.status_code == 200


def test_reassign_to_dentist_of_another_clinic_is_422(receptionist_client, monkeypatch):
    """El odontólogo nuevo tiene que ser de esta clínica: un id ajeno o inexistente no entra."""
    import routers.appointments as ra
    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: {1: 1, 2: 1, 77: 2}.get(uid))
    apt_id = _insert(dentist_user_id=1, days_ahead=45)

    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 77}).status_code == 422
    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 4242}).status_code == 422
    assert _fila(apt_id)["dentist_user_id"] == 1
    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2}).status_code == 200


def test_create_with_dentist_of_another_clinic_is_422(receptionist_client, monkeypatch):
    import routers.appointments as ra
    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: {1: 1, 77: 2}.get(uid))
    start, end = future_slot(days_ahead=46)
    res = receptionist_client.post(f"{BASE}/", json={
        "dentist_user_id": 77, "patient_user_id": 6,
        "start_time_utc": start, "end_time_utc": end,
    })
    assert res.status_code == 422
    assert "clinic" in res.json()["detail"].lower()


def test_dentist_id_must_be_positive(receptionist_client):
    apt_id = _insert(dentist_user_id=1, days_ahead=47)
    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 0}).status_code == 422
    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": -7}).status_code == 422


def test_update_from_other_clinic_is_404(other_clinic_client):
    apt_id = _insert(clinic_id=1, days_ahead=48)
    assert other_clinic_client.put(f"{BASE}/{apt_id}", json={"reason": "x"}).status_code == 404


def test_reactivating_with_existing_google_event_does_not_recreate(receptionist_client, monkeypatch):
    """Ausente → programado: el evento nunca se borró, así que no se crea otro."""
    llamadas = _con_google(monkeypatch, [1])
    apt_id = _insert(days_ahead=49, status=AppointmentStatus.NO_SHOW, google_event_id="evt-sigue")
    assert _patch(receptionist_client, apt_id, "SCHEDULED").status_code == 200
    assert llamadas == []
    assert _fila(apt_id)["google_event_id"] == "evt-sigue"


def test_rescheduling_leaves_an_audit_row(receptionist_client):
    apt_id = _insert(dentist_user_id=1, days_ahead=50)
    start, end = future_slot(days_ahead=51)
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"start_time_utc": start, "end_time_utc": end})
    assert res.status_code == 200
    filas = _auditoria(apt_id)
    assert len(filas) == 1
    assert filas[0][2].startswith("Horario cambiado: ")


def test_reassign_uses_each_dentists_own_calendar(receptionist_client, monkeypatch):
    """El borrado va al calendario del anterior y la creación al del nuevo: no al revés."""
    _con_google(monkeypatch, [1, 2])
    import routers.appointments as ra
    calendarios = []
    monkeypatch.setattr(ra, "get_calendar_service", lambda a, r, e: f"cal-de-{r}")
    monkeypatch.setattr(ra, "delete_google_event",
                        lambda service, cal, event_id: calendarios.append(("delete", service, event_id)))

    def _create(service, *a, **k):
        calendarios.append(("create", service))
        return "evt-nuevo"

    monkeypatch.setattr(ra, "create_google_event", _create)
    db = _db()
    for cfg in db.query(DentistCalendarConfig).all():
        cfg.google_refresh_token = f"refresh-{cfg.dentist_user_id}"
    db.commit()
    db.close()

    apt_id = _insert(dentist_user_id=1, days_ahead=52, google_event_id="evt-del-1")
    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2}).status_code == 200
    assert calendarios == [("delete", "cal-de-refresh-1", "evt-del-1"), ("create", "cal-de-refresh-2")]


def _google_que_falla(monkeypatch, dentist_ids):
    llamadas = _con_google(monkeypatch, dentist_ids)
    import routers.appointments as ra

    def _rompe(*a, **k):
        llamadas.append(("delete-FAIL",))
        raise RuntimeError("Google caído")

    monkeypatch.setattr(ra, "delete_google_event", _rompe)
    return llamadas


def test_cancel_with_google_down_keeps_event_id_and_marks_failed(receptionist_client, monkeypatch):
    """Si Google no borra, el evento sigue existiendo: se conserva el id y se marca FAILED."""
    _google_que_falla(monkeypatch, [1])
    apt_id = _insert(days_ahead=53, google_event_id="evt-vivo")
    res = _patch(receptionist_client, apt_id, "CANCELLED")
    assert res.status_code == 200  # la operación principal no depende de Google
    fila = _fila(apt_id)
    assert fila["status"] == AppointmentStatus.CANCELLED
    assert fila["google_event_id"] == "evt-vivo"
    assert fila["gcal_sync_status"].value == "FAILED"


def test_delete_with_google_down_is_reconcilable(receptionist_client, monkeypatch):
    """Borrado con Google caído: deleted_at puesto, id conservado, FAILED → se puede reconciliar."""
    _google_que_falla(monkeypatch, [1])
    apt_id = _insert(days_ahead=54, google_event_id="evt-fantasma")
    assert receptionist_client.delete(f"{BASE}/{apt_id}").status_code == 204
    fila = _fila(apt_id)
    assert fila["deleted_at"] is not None
    assert fila["google_event_id"] == "evt-fantasma"
    assert fila["gcal_sync_status"].value == "FAILED"


def test_reassign_with_google_down_keeps_old_event_in_audit(receptionist_client, monkeypatch):
    """
    No se pudo sacar del calendario viejo: el turno queda reasignado igual (lo decide la
    clínica, no Google), el id viejo queda en la auditoría y el estado de sync lo cuenta.
    """
    _google_que_falla(monkeypatch, [1])  # el Dr. 2 no tiene Google
    apt_id = _insert(dentist_user_id=1, days_ahead=55, google_event_id="evt-huerfano")
    assert receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2}).status_code == 200
    fila = _fila(apt_id)
    assert fila["dentist_user_id"] == 2
    assert fila["google_event_id"] is None
    assert fila["gcal_sync_status"].value == "FAILED"
    assert _auditoria(apt_id) == [
        ("SCHEDULED", "SCHEDULED", "Profesional cambiado: 1 → 2 (evento Google evt-huerfano)"),
    ]


def test_reassign_commits_before_touching_google(receptionist_client, monkeypatch):
    """
    Cuando Google recibe el borrado, la reasignación YA está en la base: el lock del
    solapamiento no se suelta con el odontólogo a medio cambiar (hallazgo MEDIO-1).
    """
    _con_google(monkeypatch, [1])
    import routers.appointments as ra
    visto = {}
    ids = {}

    def _delete(service, cal, event_id):
        visto["dentista_en_db"] = _fila(ids["apt"])["dentist_user_id"]

    monkeypatch.setattr(ra, "delete_google_event", _delete)
    ids["apt"] = _insert(dentist_user_id=1, days_ahead=56, google_event_id="evt-1")
    assert receptionist_client.put(f"{BASE}/{ids['apt']}", json={"dentist_user_id": 2}).status_code == 200
    assert visto["dentista_en_db"] == 2
