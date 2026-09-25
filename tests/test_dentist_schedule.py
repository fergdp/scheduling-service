"""Horario de atención por odontólogo y bloqueos de agenda (#296)."""
import pytest
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from models import Appointment, AppointmentStatus, DentistScheduleBlock, DentistScheduleSlot

_HUSO = ZoneInfo("America/Argentina/Buenos_Aires")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _futuro(dias=1, hora=9, minuto=0):
    """
    Hora LOCAL de la clínica (Argentina), con tzinfo REAL a propósito — no un datetime naive.

    ⚠️ Un naive lo toma `_a_utc` como si YA fuera UTC (no convierte): horario y turno quedarían
    comparados en el mismo reloj por construcción, y el bug de huso que encontró el review del
    #296 (`_check_dentro_de_horario` comparaba UTC crudo contra franjas locales) habría pasado
    en verde igual. Con tzinfo real, `.isoformat()` manda el offset y la API convierte de
    verdad — estos tests ejercitan la conversión, no la esquivan.
    """
    return (datetime.now(_HUSO) + timedelta(days=dias)).replace(
        hour=hora, minute=minuto, second=0, microsecond=0
    )


def _naive_utc(dt: datetime) -> datetime:
    """El mismo instante que `_a_utc`/`_naive` dejan guardado en la DB — para los helpers que
    escriben directo a la tabla, sin pasar por la API."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _cargar_horario(client, dentist_user_id, franjas):
    """franjas: lista de (weekday, 'HH:MM', 'HH:MM')."""
    res = client.put(
        f"/clinic-scheduling-api/v1/dentists/{dentist_user_id}/schedule",
        json={"slots": [{"weekday": w, "start_time": s, "end_time": e} for w, s, e in franjas]},
    )
    assert res.status_code == 200, res.json()
    return res.json()


def _crear_turno(client, dentist_user_id, start: datetime, end: datetime, patient_user_id=5):
    return client.post(
        "/clinic-scheduling-api/v1/appointments/",
        json={
            "dentist_user_id": dentist_user_id,
            "patient_user_id": patient_user_id,
            "start_time_utc": start.isoformat(),
            "end_time_utc": end.isoformat(),
        },
    )


def _insertar_turno_db(dentist_user_id, start, end, status=AppointmentStatus.SCHEDULED, clinic_id=1,
                       deleted_at=None):
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    apt = Appointment(
        clinic_id=clinic_id, dentist_user_id=dentist_user_id, patient_user_id=5,
        start_time_utc=_naive_utc(start), end_time_utc=_naive_utc(end), status=status,
        deleted_at=deleted_at,
    )
    db.add(apt)
    db.commit()
    apt_id = apt.appointment_id
    db.close()
    return apt_id


def _insertar_block_db(dentist_user_id, start, end, reason="x", clinic_id=1, created_by_user_id=1):
    """
    Directo a DB, sin pasar por ningún TestClient — a propósito: usar dos fixtures `client*`
    en el mismo test pisa el `app.dependency_overrides` de la primera con el de la segunda
    (es un único dict global), porque el setup de AMBOS fixtures corre antes del cuerpo del
    test sin importar el orden en que se los nombra ahí adentro.
    """
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    block = DentistScheduleBlock(
        clinic_id=clinic_id, dentist_user_id=dentist_user_id,
        start_time_utc=_naive_utc(start), end_time_utc=_naive_utc(end), reason=reason,
        created_by_user_id=created_by_user_id,
    )
    db.add(block)
    db.commit()
    block_id = block.block_id
    db.close()
    return block_id


def _insertar_slot_db(dentist_user_id, weekday, start_time, end_time, clinic_id=1):
    """Una franja directo a DB — para armar el horario de OTRA clínica sin un segundo client."""
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    db.add(DentistScheduleSlot(
        clinic_id=clinic_id, dentist_user_id=dentist_user_id, weekday=weekday,
        start_time=start_time, end_time=end_time,
    ))
    db.commit()
    db.close()


def _contar(modelo, **filtros) -> int:
    """Cuántas filas hay en la tabla, mirando la DB y no la respuesta de la API — para probar
    lo que un endpoint NO tocó (filas de otro odontólogo o de otra clínica)."""
    from conftest import TestingSessionLocal
    db = TestingSessionLocal()
    try:
        return db.query(modelo).filter_by(**filtros).count()
    finally:
        db.close()


@pytest.fixture
def other_clinic_admin_client():
    """
    ADMIN de OTRA clínica (clinic_id=2, user_id=61). A diferencia de `other_clinic_client`
    (recepción, que el guard de rol frena con 403 ANTES de llegar a la clínica), éste pasa el
    guard de rol de los endpoints de escritura: lo único que lo puede frenar es la validación de
    que el odontólogo sea de SU clínica.
    """
    from conftest import _make_client

    def _admin():
        return {"user_id": 61, "clinic_id": 2, "roles": ["ADMIN"]}
    yield from _make_client(_admin)


# ---------------------------------------------------------------------------
# CRUD de horario semanal
# ---------------------------------------------------------------------------

def test_get_schedule_empty_by_default(client):
    """Ningún odontólogo tiene horario cargado hasta que alguien lo carga (decisión 4)."""
    res = client.get("/clinic-scheduling-api/v1/dentists/1/schedule")
    assert res.status_code == 200
    assert res.json() == {"slots": []}


def test_dentist_sets_own_schedule(other_dentist_client):
    """El odontólogo (sin ADMIN) carga el suyo propio."""
    data = _cargar_horario(other_dentist_client, 99, [(0, "09:00", "13:00"), (0, "15:00", "19:00")])
    assert len(data["slots"]) == 2
    assert {s["start_time"] for s in data["slots"]} == {"09:00:00", "15:00:00"}


def test_dentist_cannot_set_another_dentists_schedule(other_dentist_client):
    """DENTIST (user_id=99) sobre el horario de OTRO (dentist_user_id=1) → 403."""
    res = other_dentist_client.put(
        "/clinic-scheduling-api/v1/dentists/1/schedule",
        json={"slots": [{"weekday": 0, "start_time": "09:00", "end_time": "13:00"}]},
    )
    assert res.status_code == 403


def test_admin_sets_any_dentists_schedule(client):
    """`client` tiene ADMIN (además de DENTIST, user_id=1) y edita el de otro odontólogo (99)."""
    data = _cargar_horario(client, 99, [(1, "08:00", "12:00")])
    assert len(data["slots"]) == 1


def test_receptionist_reads_but_cannot_write_schedule(receptionist_client):
    """Recepción ve el horario (decisión 3) pero no lo edita."""
    assert receptionist_client.get("/clinic-scheduling-api/v1/dentists/1/schedule").status_code == 200
    res = receptionist_client.put(
        "/clinic-scheduling-api/v1/dentists/1/schedule",
        json={"slots": [{"weekday": 0, "start_time": "09:00", "end_time": "13:00"}]},
    )
    assert res.status_code == 403


def test_replace_schedule_overwrites_previous(client):
    """PUT reemplaza TODO el horario, no suma filas."""
    _cargar_horario(client, 1, [(0, "09:00", "13:00")])
    data = _cargar_horario(client, 1, [(1, "10:00", "14:00")])
    assert len(data["slots"]) == 1
    assert data["slots"][0]["weekday"] == 1


def test_replace_schedule_to_empty_removes_restriction(client):
    """Mandar `slots: []` vuelve a «disponible siempre» — no dice «sin cargar»."""
    _cargar_horario(client, 1, [(0, "09:00", "13:00")])
    data = _cargar_horario(client, 1, [])
    assert data["slots"] == []


def test_schedule_rejects_end_before_start(client):
    res = client.put(
        "/clinic-scheduling-api/v1/dentists/1/schedule",
        json={"slots": [{"weekday": 0, "start_time": "13:00", "end_time": "09:00"}]},
    )
    assert res.status_code == 422


def test_schedule_rejects_overlapping_ranges_same_day(client):
    """9-14 y 12-18 el mismo lunes se pisan: rechazado antes de guardar nada."""
    res = client.put(
        "/clinic-scheduling-api/v1/dentists/1/schedule",
        json={"slots": [
            {"weekday": 0, "start_time": "09:00", "end_time": "14:00"},
            {"weekday": 0, "start_time": "12:00", "end_time": "18:00"},
        ]},
    )
    assert res.status_code == 422
    # Nada se guardó: el rechazo es todo-o-nada.
    assert client.get("/clinic-scheduling-api/v1/dentists/1/schedule").json() == {"slots": []}


def test_schedule_allows_adjacent_ranges_same_day(client):
    """9-13 y 13-17 (pegados, sin hueco) no se consideran superpuestos."""
    data = _cargar_horario(client, 1, [(0, "09:00", "13:00"), (0, "13:00", "17:00")])
    assert len(data["slots"]) == 2


def test_cross_clinic_client_cannot_touch_schedule(other_clinic_client):
    """`_require_dentist_of_clinic` (compartido con turnos) bloquea el cross-tenant."""
    res = other_clinic_client.get("/clinic-scheduling-api/v1/dentists/1/schedule")
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# CRUD de bloqueos
# ---------------------------------------------------------------------------

def test_get_blocks_empty_by_default(client):
    inicio = _futuro(1, 0, 0)
    fin = _futuro(10, 0, 0)
    res = client.get(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        params={"from": inicio.isoformat(), "to": fin.isoformat()},
    )
    assert res.status_code == 200
    assert res.json() == {"blocks": []}


def test_dentist_creates_own_block(other_dentist_client):
    start, end = _futuro(2, 9), _futuro(2, 10)
    res = other_dentist_client.post(
        "/clinic-scheduling-api/v1/dentists/99/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "Congreso"},
    )
    assert res.status_code == 200, res.json()
    assert res.json()["reason"] == "Congreso"


def test_dentist_cannot_create_block_for_another_dentist(other_dentist_client):
    start, end = _futuro(2, 9), _futuro(2, 10)
    res = other_dentist_client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "Vacaciones"},
    )
    assert res.status_code == 403


def test_receptionist_cannot_create_block(receptionist_client):
    start, end = _futuro(2, 9), _futuro(2, 10)
    res = receptionist_client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "x"},
    )
    assert res.status_code == 403


def test_block_rejects_end_before_start(client):
    start, end = _futuro(2, 10), _futuro(2, 9)
    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "x"},
    )
    assert res.status_code == 422


def test_block_requires_reason(client):
    start, end = _futuro(2, 9), _futuro(2, 10)
    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": ""},
    )
    assert res.status_code == 422


def test_block_rejects_whitespace_only_reason(client):
    """" "   " pasa min_length=1 pero no dice nada — se rechaza igual que vacío."""
    start, end = _futuro(2, 9), _futuro(2, 10)
    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "   "},
    )
    assert res.status_code == 422


def test_block_over_active_appointment_is_rejected(client):
    """Decisión 2: un bloqueo que pisa un turno ACTIVO se rechaza, con la lista de turnos."""
    start, end = _futuro(2, 9), _futuro(2, 10)
    apt_id = _insertar_turno_db(1, start, end, status=AppointmentStatus.SCHEDULED)

    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "Vacaciones"},
    )
    assert res.status_code == 409
    ids_pisados = [t["appointment_id"] for t in res.json()["detail"]["conflicting_appointments"]]
    assert apt_id in ids_pisados


def test_block_over_cancelled_appointment_is_allowed(client):
    """Un CANCELLED no ocupa el hueco: no bloquea que se cree el bloqueo."""
    start, end = _futuro(2, 9), _futuro(2, 10)
    _insertar_turno_db(1, start, end, status=AppointmentStatus.CANCELLED)

    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "Vacaciones"},
    )
    assert res.status_code == 200


def test_delete_block(client):
    start, end = _futuro(2, 9), _futuro(2, 10)
    block_id = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "x"},
    ).json()["block_id"]

    res = client.delete(f"/clinic-scheduling-api/v1/dentists/1/blocks/{block_id}")
    assert res.status_code == 204

    listado = client.get(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        params={"from": _futuro(0, 0).isoformat(), "to": _futuro(10, 0).isoformat()},
    ).json()
    assert listado["blocks"] == []


def test_delete_block_not_found(client):
    res = client.delete("/clinic-scheduling-api/v1/dentists/1/blocks/999999")
    assert res.status_code == 404


def test_dentist_cannot_delete_another_dentists_block(other_dentist_client):
    start, end = _futuro(2, 9), _futuro(2, 10)
    block_id = _insertar_block_db(1, start, end)

    res = other_dentist_client.delete(f"/clinic-scheduling-api/v1/dentists/1/blocks/{block_id}")
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Los turnos respetan el horario y los bloqueos
# ---------------------------------------------------------------------------

def test_appointment_without_schedule_is_unrestricted(client):
    """Decisión 4: sin ninguna fila cargada, cualquier horario vale — como siempre."""
    start, end = _futuro(1, 9, 0), _futuro(1, 9, 30)
    res = _crear_turno(client, 1, start, end)
    assert res.status_code == 200, res.json()


def test_appointment_within_schedule_succeeds(client):
    start = _futuro(1, 10, 0)
    end = start + timedelta(minutes=30)
    _cargar_horario(client, 1, [(start.weekday(), "09:00", "13:00")])

    res = _crear_turno(client, 1, start, end)
    assert res.status_code == 200, res.json()


def test_appointment_at_exact_schedule_boundary_succeeds(client):
    """Un turno que empieza/termina EXACTO en el borde de la franja (9:00-9:30 dentro de
    9:00-13:00) entra — el borde es inclusivo, no hay que dejar un margen."""
    start = _futuro(1, 9, 0)
    end = start + timedelta(minutes=30)
    _cargar_horario(client, 1, [(start.weekday(), "09:00", "13:00")])

    res = _crear_turno(client, 1, start, end)
    assert res.status_code == 200, res.json()


def test_appointment_outside_schedule_is_rejected(client):
    """El odontólogo SÓLO atiende 9-13 ese día; un turno a las 15hs se rechaza."""
    start = _futuro(1, 15, 0)
    end = start + timedelta(minutes=30)
    _cargar_horario(client, 1, [(start.weekday(), "09:00", "13:00")])

    res = _crear_turno(client, 1, start, end)
    assert res.status_code == 409
    assert "horario" in res.json()["detail"].lower()


def test_appointment_check_converts_utc_to_local_before_comparing(client):
    """
    Regresión puntual del hallazgo de review: comparar la hora UTC del turno contra las franjas
    (que están en hora LOCAL) sin convertir queda corrido por el offset de Argentina (-3h).

    Con el horario 09:00-13:00 local: 12:30 local es un turno válido (12:30 local = 15:30 UTC;
    sin la conversión, 15:30 se compararía crudo contra 09:00-13:00 y se rechazaría). Y 07:00
    local está ANTES de que abra (07:00 local = 10:00 UTC; sin la conversión, 10:00 caería
    "dentro" de 09:00-13:00 y se aceptaría un turno fuera de horario).
    """
    dia = _futuro(1, 12, 30).weekday()
    _cargar_horario(client, 1, [(dia, "09:00", "13:00")])

    adentro = _futuro(1, 12, 30)
    res_adentro = _crear_turno(client, 1, adentro, adentro + timedelta(minutes=30))
    assert res_adentro.status_code == 200, res_adentro.json()

    afuera = _futuro(1, 7, 0)
    res_afuera = _crear_turno(client, 1, afuera, afuera + timedelta(minutes=30))
    assert res_afuera.status_code == 409, res_afuera.json()


def test_appointment_on_day_off_is_rejected(client):
    """Horario cargado lunes a viernes, sin fila el fin de semana → ese día no atiende."""
    start = _futuro(1, 10, 0)
    otro_dia = (start.weekday() + 1) % 7  # cualquier día DISTINTO al del turno
    end = start + timedelta(minutes=30)
    _cargar_horario(client, 1, [(otro_dia, "09:00", "18:00")])

    res = _crear_turno(client, 1, start, end)
    assert res.status_code == 409


def test_appointment_spanning_lunch_gap_is_rejected(client):
    """9-13 y 15-19: un turno de 12:45 a 13:15 no entra ENTERO en ninguna de las dos franjas."""
    start = _futuro(1, 12, 45)
    end = start + timedelta(minutes=30)  # 13:15
    _cargar_horario(client, 1, [(start.weekday(), "09:00", "13:00"), (start.weekday(), "15:00", "19:00")])

    res = _crear_turno(client, 1, start, end)
    assert res.status_code == 409


def test_appointment_over_block_is_rejected(client):
    start, end = _futuro(2, 9), _futuro(2, 10)
    client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "Vacaciones"},
    )

    res = _crear_turno(client, 1, start + timedelta(minutes=15), start + timedelta(minutes=45))
    assert res.status_code == 409
    assert "bloqueado" in res.json()["detail"].lower()


def test_update_appointment_into_blocked_time_is_rejected(client):
    """Mover un turno existente a un horario bloqueado también se rechaza."""
    original_start, original_end = _futuro(1, 9), _futuro(1, 9, 30)
    apt_id = _crear_turno(client, 1, original_start, original_end).json()["appointment_id"]

    block_start, block_end = _futuro(2, 9), _futuro(2, 10)
    client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": block_start.isoformat(), "end_time_utc": block_end.isoformat(), "reason": "x"},
    )

    res = client.put(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}",
        json={"start_time_utc": (block_start + timedelta(minutes=15)).isoformat()},
    )
    assert res.status_code == 409


def test_reactivating_appointment_into_now_blocked_time_is_rejected(client):
    """Reactivar un CANCELLED (vuelve a un estado activo) re-chequea horario y bloqueos."""
    start, end = _futuro(2, 9), _futuro(2, 10)
    apt_id = _insertar_turno_db(1, start, end, status=AppointmentStatus.CANCELLED)

    client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "x"},
    )

    res = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "SCHEDULED"},
    )
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# Bordes, aislamiento y cableado
#
# Estos tests salieron de correr mutantes sobre los checks (cambiar `<` por `<=`, sacar un
# filtro, dejar de llamar al check al editar o al reactivar): sin ellos sobrevivían seis, o sea
# que ese código se podía romper y la suite seguía en verde.
# ---------------------------------------------------------------------------

def test_appointment_touching_a_block_edge_is_allowed(client):
    """Bloqueo 10:00-11:00: un turno que TERMINA a las 10:00 o EMPIEZA a las 11:00 no lo pisa
    — el mismo criterio que entre turnos: pegados no se pisan."""
    inicio, fin = _futuro(2, 10), _futuro(2, 11)
    _insertar_block_db(1, inicio, fin)

    antes = _crear_turno(client, 1, _futuro(2, 9, 30), inicio)
    assert antes.status_code == 200, antes.json()
    despues = _crear_turno(client, 1, fin, _futuro(2, 11, 30))
    assert despues.status_code == 200, despues.json()


def test_block_touching_an_appointment_edge_is_allowed(client):
    """El espejo: con un turno 10:00-11:00, un bloqueo que termina a las 10:00 o empieza a las
    11:00 no lo pisa, así que no se rechaza."""
    inicio, fin = _futuro(2, 10), _futuro(2, 11)
    _insertar_turno_db(1, inicio, fin)

    for desde, hasta in ((_futuro(2, 9), inicio), (fin, _futuro(2, 12))):
        res = client.post(
            "/clinic-scheduling-api/v1/dentists/1/blocks",
            json={"start_time_utc": desde.isoformat(), "end_time_utc": hasta.isoformat(), "reason": "x"},
        )
        assert res.status_code == 200, res.json()


def test_schedule_of_one_dentist_does_not_open_hours_for_another(client):
    """El horario es POR odontólogo: las franjas de uno no le abren turnos a otro."""
    dia = _futuro(1, 10).weekday()
    _cargar_horario(client, 1, [(dia, "09:00", "13:00")])
    _cargar_horario(client, 99, [(dia, "15:00", "19:00")])

    tarde = _futuro(1, 15, 0)  # entra en el horario del 99, no en el del 1
    res = _crear_turno(client, 1, tarde, tarde + timedelta(minutes=30))
    assert res.status_code == 409, res.json()

    manana = _futuro(1, 10, 0)  # entra en el horario del 1, no en el del 99
    res = _crear_turno(client, 99, manana, manana + timedelta(minutes=30))
    assert res.status_code == 409, res.json()


def test_block_of_one_dentist_does_not_block_another(client):
    """El bloqueo es POR odontólogo: las vacaciones de uno no cierran la agenda de otro."""
    inicio, fin = _futuro(2, 9), _futuro(2, 10)
    _insertar_block_db(99, inicio, fin, reason="Vacaciones")

    res = _crear_turno(client, 1, inicio, fin)
    assert res.status_code == 200, res.json()


def test_block_ignores_appointments_of_another_dentist(client):
    """Los turnos de OTRO odontólogo no impiden bloquear la agenda de éste."""
    inicio, fin = _futuro(2, 9), _futuro(2, 10)
    _insertar_turno_db(99, inicio, fin)

    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": inicio.isoformat(), "end_time_utc": fin.isoformat(), "reason": "x"},
    )
    assert res.status_code == 200, res.json()


def test_block_over_deleted_appointment_is_allowed(client):
    """Un turno borrado (soft delete) conserva su status viejo pero ya no ocupa el hueco."""
    inicio, fin = _futuro(2, 9), _futuro(2, 10)
    _insertar_turno_db(1, inicio, fin, status=AppointmentStatus.SCHEDULED,
                       deleted_at=datetime.now(timezone.utc).replace(tzinfo=None))

    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": inicio.isoformat(), "end_time_utc": fin.isoformat(), "reason": "x"},
    )
    assert res.status_code == 200, res.json()


def test_other_clinic_schedule_does_not_restrict_this_clinic(client):
    """Multi-tenant: una franja de OTRA clínica con el mismo `dentist_user_id` no cuenta — en
    ésta el odontólogo no cargó nada y sigue disponible siempre."""
    inicio = _futuro(2, 10)
    _insertar_slot_db(1, inicio.weekday(), time(15, 0), time(19, 0), clinic_id=2)

    res = _crear_turno(client, 1, inicio, inicio + timedelta(minutes=30))
    assert res.status_code == 200, res.json()


def test_other_clinic_slot_does_not_open_hours_in_this_clinic(client):
    """El otro lado: si el odontólogo SÍ tiene horario en ésta (9-13), una franja de OTRA
    clínica con el mismo `dentist_user_id` (15-19) no le abre las 15hs."""
    inicio = _futuro(1, 15, 0)
    _cargar_horario(client, 1, [(inicio.weekday(), "09:00", "13:00")])
    _insertar_slot_db(1, inicio.weekday(), time(15, 0), time(19, 0), clinic_id=2)

    res = _crear_turno(client, 1, inicio, inicio + timedelta(minutes=30))
    assert res.status_code == 409, res.json()


def test_other_clinic_block_does_not_block_this_clinic(client):
    inicio, fin = _futuro(2, 10), _futuro(2, 11)
    _insertar_block_db(1, inicio, fin, clinic_id=2)

    res = _crear_turno(client, 1, inicio, fin)
    assert res.status_code == 200, res.json()


def test_block_ignores_active_appointments_of_another_clinic(client):
    inicio, fin = _futuro(2, 10), _futuro(2, 11)
    _insertar_turno_db(1, inicio, fin, clinic_id=2)

    res = client.post(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        json={"start_time_utc": inicio.isoformat(), "end_time_utc": fin.isoformat(), "reason": "x"},
    )
    assert res.status_code == 200, res.json()


def test_update_appointment_out_of_schedule_is_rejected(client):
    """Mover un turno existente fuera del horario del odontólogo se rechaza: el mismo check que
    al crear, también al editar (el E2E HO3 lo cubre contra el stack real; esto lo ata acá)."""
    start = _futuro(1, 10, 0)
    _cargar_horario(client, 1, [(start.weekday(), "09:00", "13:00")])
    apt_id = _crear_turno(client, 1, start, start + timedelta(minutes=30)).json()["appointment_id"]

    afuera = _futuro(1, 15, 0)
    res = client.put(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}",
        json={
            "start_time_utc": afuera.isoformat(),
            "end_time_utc": (afuera + timedelta(minutes=30)).isoformat(),
        },
    )
    assert res.status_code == 409, res.json()
    assert "horario" in res.json()["detail"].lower()


def test_reactivating_appointment_out_of_schedule_is_rejected(client):
    """Un CANCELLED que quedó fuera del horario (el odontólogo lo cambió después) no se reactiva
    sin re-chequear: vuelve a ocupar el hueco, así que vale lo mismo que al crear."""
    start = _futuro(2, 15, 0)
    apt_id = _insertar_turno_db(1, start, start + timedelta(minutes=30), status=AppointmentStatus.CANCELLED)
    _cargar_horario(client, 1, [(start.weekday(), "09:00", "13:00")])

    res = client.patch(
        f"/clinic-scheduling-api/v1/appointments/{apt_id}/status",
        json={"status": "SCHEDULED"},
    )
    assert res.status_code == 409, res.json()
    assert "horario" in res.json()["detail"].lower()


# ---------------------------------------------------------------------------
# CRUD: filtros por odontólogo y clínica, orden y guards de clínica
#
# Segunda tanda de mutantes, ahora sobre routers/dentist_schedule.py: sobrevivían 20 de 25 (un
# listado de bloqueos que devolvía siempre vacío pasaba toda la suite). Los tests de arriba sólo
# miraban el caso feliz de UN odontólogo en UNA clínica.
# ---------------------------------------------------------------------------

_FRANJAS_DESORDENADAS = [(3, "09:00", "13:00"), (1, "15:00", "19:00"), (1, "09:00", "13:00")]
_FRANJAS_ORDENADAS = [(1, "09:00:00"), (1, "15:00:00"), (3, "09:00:00")]


def _dia_y_hora(franjas):
    return [(s["weekday"], s["start_time"]) for s in franjas]


def test_get_schedule_returns_only_this_dentist_and_clinic_sorted(client):
    """Otro odontólogo y otra clínica con el mismo `dentist_user_id` no se cuelan en la
    respuesta, y sale ordenada por día y hora aunque se haya cargado desordenada."""
    _cargar_horario(client, 99, [(2, "09:00", "13:00")])
    _insertar_slot_db(1, 4, time(8, 0), time(12, 0), clinic_id=2)
    _cargar_horario(client, 1, _FRANJAS_DESORDENADAS)

    res = client.get("/clinic-scheduling-api/v1/dentists/1/schedule")
    assert _dia_y_hora(res.json()["slots"]) == _FRANJAS_ORDENADAS


def test_replace_schedule_touches_only_this_dentist_and_clinic(client):
    """El PUT reemplaza SÓLO lo de este odontólogo en esta clínica: no pisa el horario de un
    colega ni las filas del mismo `dentist_user_id` en otra clínica, y su respuesta trae sólo
    lo recién cargado, ordenado."""
    _cargar_horario(client, 99, [(2, "09:00", "13:00")])
    _insertar_slot_db(1, 4, time(8, 0), time(12, 0), clinic_id=2)

    data = _cargar_horario(client, 1, _FRANJAS_DESORDENADAS)

    assert _dia_y_hora(data["slots"]) == _FRANJAS_ORDENADAS
    assert _contar(DentistScheduleSlot, dentist_user_id=99, clinic_id=1) == 1
    assert _contar(DentistScheduleSlot, dentist_user_id=1, clinic_id=2) == 1


def test_list_blocks_filters_by_dentist_clinic_and_range_sorted(client):
    """Lista SÓLO los bloqueos de este odontólogo y clínica que pisan el rango: los pegados al
    borde (terminan justo cuando arranca, o arrancan justo cuando termina) quedan afuera, los
    que lo pisan a medias entran, y sale ordenado por inicio."""
    d = 2
    _insertar_block_db(1, _futuro(d, 10), _futuro(d, 11), reason="dentro")
    _insertar_block_db(1, _futuro(d, 8, 30), _futuro(d, 9, 30), reason="parcial")
    _insertar_block_db(1, _futuro(d, 8), _futuro(d, 9), reason="pegado antes")
    _insertar_block_db(1, _futuro(d, 12), _futuro(d, 13), reason="pegado después")
    _insertar_block_db(99, _futuro(d, 10), _futuro(d, 11), reason="otro odontólogo")
    _insertar_block_db(1, _futuro(d, 10), _futuro(d, 11), reason="otra clínica", clinic_id=2)

    res = client.get(
        "/clinic-scheduling-api/v1/dentists/1/blocks",
        params={"from": _futuro(d, 9).isoformat(), "to": _futuro(d, 12).isoformat()},
    )
    assert res.status_code == 200, res.json()
    assert [b["reason"] for b in res.json()["blocks"]] == ["parcial", "dentro"]


def test_block_records_who_created_it(client):
    """Un ADMIN (user 1) bloquea la agenda del odontólogo 99: queda registrado quién lo hizo,
    no el dueño de la agenda."""
    start, end = _futuro(2, 9), _futuro(2, 10)
    res = client.post(
        "/clinic-scheduling-api/v1/dentists/99/blocks",
        json={"start_time_utc": start.isoformat(), "end_time_utc": end.isoformat(), "reason": "Congreso"},
    )
    assert res.status_code == 200, res.json()
    assert _contar(DentistScheduleBlock, dentist_user_id=99, created_by_user_id=1, clinic_id=1) == 1


def test_delete_block_ignores_blocks_of_another_dentist_or_clinic(client):
    """El ADMIN borra bloqueos del odontólogo de la URL, no cualquier `block_id`: uno de otro
    odontólogo o de otra clínica da 404 y sigue existiendo."""
    ajeno = _insertar_block_db(99, _futuro(2, 9), _futuro(2, 10))
    de_otra_clinica = _insertar_block_db(1, _futuro(2, 9), _futuro(2, 10), clinic_id=2)

    assert client.delete(f"/clinic-scheduling-api/v1/dentists/1/blocks/{ajeno}").status_code == 404
    assert client.delete(f"/clinic-scheduling-api/v1/dentists/1/blocks/{de_otra_clinica}").status_code == 404
    assert _contar(DentistScheduleBlock) == 2


def test_other_clinic_admin_cannot_touch_this_clinics_dentist(other_clinic_admin_client):
    """Multi-tenant: un ADMIN de OTRA clínica pasa el guard de rol, pero el odontólogo 1 no es
    de la suya → 422 en los cinco endpoints y no se escribe nada."""
    c = other_clinic_admin_client
    base = "/clinic-scheduling-api/v1/dentists/1"
    inicio, fin = _futuro(2, 9), _futuro(2, 10)

    assert c.get(f"{base}/schedule").status_code == 422
    assert c.put(
        f"{base}/schedule",
        json={"slots": [{"weekday": 0, "start_time": "09:00", "end_time": "13:00"}]},
    ).status_code == 422
    assert c.get(
        f"{base}/blocks", params={"from": _futuro(1, 0).isoformat(), "to": _futuro(10, 0).isoformat()},
    ).status_code == 422
    assert c.post(
        f"{base}/blocks",
        json={"start_time_utc": inicio.isoformat(), "end_time_utc": fin.isoformat(), "reason": "x"},
    ).status_code == 422
    assert c.delete(f"{base}/blocks/1").status_code == 422
    assert _contar(DentistScheduleSlot) == 0
    assert _contar(DentistScheduleBlock) == 0
