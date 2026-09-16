"""
El filtro de estado de la agenda (#302) y la paginación del calendario (#288).

Lo que fija este archivo:

1. **`status` acepta varios valores.** «Por atender» son tres estados; con uno solo, la lista
   tendría que pedirlos por separado o filtrar en el navegador sobre los últimos 200 turnos.
2. **Un solo `status` sigue igual**, y uno inválido sigue siendo 422: el cambio es aditivo.
3. **Filtrar por estado no ensancha el alcance** de un odontólogo ni de un paciente.
4. **El orden tiene desempate.** El calendario pide un mes por páginas, y con varios turnos a la
   misma hora MySQL puede devolver las filas empatadas en distinto orden en cada página.
"""
from datetime import datetime, timedelta

from sqlalchemy import event

from conftest import engine
from models import Appointment, AppointmentStatus
from test_agenda_recepcion import _db, _insert

BASE = "/clinic-scheduling-api/v1/appointments"
POR_ATENDER = ["SCHEDULED", "CONFIRMED", "ARRIVED"]


def _uno_de_cada_estado(**extra):
    """Seis turnos, uno por estado, en días distintos. Devuelve {estado: id}."""
    return {
        s.value: _insert(days_ahead=i + 1, status=s, **extra)
        for i, s in enumerate(AppointmentStatus)
    }


def _ids(res):
    assert res.status_code == 200, res.text
    return sorted(t["appointment_id"] for t in res.json()["appointments"])


# ---------------------------------------------------------------------------
# 1 y 2. Varios estados, uno solo, uno inválido
# ---------------------------------------------------------------------------

def test_varios_estados_traen_los_de_cualquiera_de_ellos(receptionist_client):
    """«Por atender»: programado, confirmado y en espera, y ninguno de los que cierran el turno."""
    ids = _uno_de_cada_estado()

    res = receptionist_client.get(f"{BASE}/", params={"status": POR_ATENDER})

    assert _ids(res) == sorted(ids[s] for s in POR_ATENDER), res.json()
    assert res.json()["total"] == 3


def test_un_solo_estado_sigue_funcionando_como_antes(receptionist_client):
    """
    El control de que el cambio es aditivo: el front viejo, la historia clínica y cualquier
    llamada existente mandan un único `status`.
    """
    ids = _uno_de_cada_estado()

    res = receptionist_client.get(f"{BASE}/", params={"status": "CANCELLED"})

    assert _ids(res) == [ids["CANCELLED"]]


def test_un_estado_que_no_existe_es_422_aunque_venga_con_otros_validos(receptionist_client):
    """Un valor inválido no se ignora en silencio: la lista entera se rechaza."""
    res = receptionist_client.get(f"{BASE}/", params={"status": ["SCHEDULED", "PENDIENTE"]})
    assert res.status_code == 422, res.text


# ---------------------------------------------------------------------------
# 3. El filtro se suma al alcance, nunca lo reemplaza
# ---------------------------------------------------------------------------

def test_filtrar_por_estados_no_le_muestra_a_un_odontologo_la_agenda_de_un_colega(other_dentist_client):
    """`other_dentist_client` es el odontólogo 99: con «Por atender» sigue viendo sólo lo suyo."""
    _uno_de_cada_estado(dentist_user_id=1)
    propio = _insert(dentist_user_id=99, days_ahead=9, status=AppointmentStatus.CONFIRMED)

    res = other_dentist_client.get(f"{BASE}/", params={"status": POR_ATENDER})

    assert _ids(res) == [propio]


def test_filtrar_por_estados_no_le_muestra_a_un_paciente_turnos_ajenos(patient_client):
    """`patient_client` es el paciente 10."""
    _uno_de_cada_estado(patient_user_id=5)
    propio = _insert(patient_user_id=10, days_ahead=9, status=AppointmentStatus.SCHEDULED)

    res = patient_client.get(f"{BASE}/", params={"status": POR_ATENDER})

    assert _ids(res) == [propio]


def test_se_combina_con_el_profesional_y_con_el_rango(receptionist_client):
    """
    Lo que manda el calendario de la recepción: un odontólogo, una semana, y estados. Cada uno
    de los tres filtros deja afuera algo que los otros dos dejarían pasar.
    """
    _uno_de_cada_estado(dentist_user_id=1)                                  # otro odontólogo
    del_dos = _uno_de_cada_estado(dentist_user_id=2)                        # días 1 a 6
    _insert(dentist_user_id=2, days_ahead=20, status=AppointmentStatus.SCHEDULED)  # fuera del rango

    res = receptionist_client.get(f"{BASE}/", params={
        "status": POR_ATENDER,
        "dentist_user_id": 2,
        "date_from": datetime.now().isoformat(),
        "date_to": (datetime.now() + timedelta(days=7)).isoformat(),
    })

    assert _ids(res) == sorted(del_dos[s] for s in POR_ATENDER)


# ---------------------------------------------------------------------------
# 4. Paginar la agenda: el desempate del orden
# ---------------------------------------------------------------------------

def _mismo_horario(cantidad, inicio):
    """`cantidad` turnos a la MISMA hora, cada uno con su odontólogo: las filas empatadas."""
    db = _db()
    try:
        filas = [
            Appointment(
                clinic_id=1, patient_user_id=5, dentist_user_id=100 + i,
                patient_name="Paciente Test",
                start_time_utc=inicio, end_time_utc=inicio + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )
            for i in range(cantidad)
        ]
        db.add_all(filas)
        db.commit()
        return sorted(f.appointment_id for f in filas)
    finally:
        db.close()


def test_un_rango_pedido_por_paginas_trae_cada_turno_una_sola_vez(receptionist_client):
    """
    El camino del calendario (#288): siete turnos a la misma hora, de a tres por página. Cada
    turno tiene que salir exactamente una vez, y `total` tiene que ser el mismo en cada página,
    porque el front decide con él si pide la siguiente.

    ⚠️ En SQLite esto pasa aunque el orden no tenga desempate: devuelve las filas empatadas en
    el orden en que las guardó. El que atrapa la falta de desempate es el test de abajo.
    """
    inicio = datetime.now().replace(microsecond=0) + timedelta(days=3)
    ids = _mismo_horario(7, inicio)
    rango = {
        "date_from": (inicio - timedelta(hours=1)).isoformat(),
        "date_to": (inicio + timedelta(hours=1)).isoformat(),
        "limit": 3,
    }

    vistos = []
    for offset in (0, 3, 6):
        res = receptionist_client.get(f"{BASE}/", params={**rango, "offset": offset})
        assert res.json()["total"] == 7
        vistos += [t["appointment_id"] for t in res.json()["appointments"]]

    assert sorted(vistos) == ids, f"páginas con repetidos o faltantes: {vistos}"


def _sql_del_listado(cliente, params):
    """Corre el listado y devuelve el SELECT paginado que llegó a la base."""
    consultas = []

    def _anotar(conn, cursor, statement, parameters, context, executemany):
        consultas.append(statement)

    event.listen(engine, "before_cursor_execute", _anotar)
    try:
        res = cliente.get(f"{BASE}/", params=params)
    finally:
        event.remove(engine, "before_cursor_execute", _anotar)
    assert res.status_code == 200, res.text
    paginadas = [q for q in consultas if "LIMIT" in q and "FROM appointments" in q]
    assert len(paginadas) == 1, consultas
    return " ".join(paginadas[0].split())


def test_la_agenda_por_rango_desempata_por_id(receptionist_client):
    """
    SQLite no reproduce el desorden de MySQL con filas empatadas, así que en vez del resultado
    se mira la consulta: el `ORDER BY` tiene que terminar en el id.
    """
    sql = _sql_del_listado(receptionist_client, {
        "date_from": datetime.now().isoformat(),
        "date_to": (datetime.now() + timedelta(days=7)).isoformat(),
    })
    assert "ORDER BY appointments.start_time_utc ASC, appointments.appointment_id ASC" in sql, sql


def test_el_historial_tambien_desempata_por_id(receptionist_client):
    """Sin rango el orden va al revés, y el desempate también."""
    sql = _sql_del_listado(receptionist_client, {})
    assert "ORDER BY appointments.start_time_utc DESC, appointments.appointment_id DESC" in sql, sql
