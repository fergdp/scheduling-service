"""
El teléfono vigente del paciente en las respuestas de turnos (issue #313).

El turno guarda `patient_phone` al crearse y nada lo actualizaba después: si el paciente
cambiaba de número y lo corregían en su ficha, Llamar y el recordatorio por WhatsApp (#295)
seguían yendo al viejo. Las rutas de lectura ahora reemplazan `patient_phone` por el que
`users` tiene hoy, y sólo caen al guardado si el paciente no tiene uno cargado.

`conftest.sin_telefonos_vigentes` deja el mapa vacío por defecto (la base de tests no tiene
`users`), así que todo lo demás en la suite sigue viendo el guardado sin cambios; acá se pisa
ese mapa a mano para probar el reemplazo.
"""
from test_agenda_recepcion import BASE, _fila, _insert
from test_appointments import future_slot


def _con_telefonos(monkeypatch, mapa):
    """Reemplaza `_telefonos_vigentes` en el router y devuelve la lista de llamadas recibidas."""
    import routers.appointments as ra
    llamadas = []

    def _fake(patient_user_ids):
        llamadas.append(set(patient_user_ids))
        return dict(mapa)

    monkeypatch.setattr(ra, "_telefonos_vigentes", _fake)
    return llamadas


def test_list_muestra_el_telefono_vigente_no_el_guardado(client, monkeypatch):
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja")
    _con_telefonos(monkeypatch, {5: "351-1111-nueva"})

    res = client.get(f"{BASE}/")
    assert res.status_code == 200
    fila = next(a for a in res.json()["appointments"] if a["appointment_id"] == apt_id)
    assert fila["patient_phone"] == "351-1111-nueva"
    # El guardado en la base no se toca: es el respaldo si mañana no hay vigente.
    assert _fila(apt_id)["patient_phone"] == "011-0000-vieja"


def test_detalle_muestra_el_telefono_vigente(client, monkeypatch):
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja")
    _con_telefonos(monkeypatch, {5: "351-1111-nueva"})

    res = client.get(f"{BASE}/{apt_id}")
    assert res.status_code == 200
    assert res.json()["patient_phone"] == "351-1111-nueva"


def test_upcoming_muestra_el_telefono_vigente(client, monkeypatch):
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja", days_ahead=1)
    _con_telefonos(monkeypatch, {5: "351-1111-nueva"})

    res = client.get(f"{BASE}/upcoming")
    assert res.status_code == 200
    fila = next(a for a in res.json()["appointments"] if a["appointment_id"] == apt_id)
    assert fila["patient_phone"] == "351-1111-nueva"


def test_sin_telefono_vigente_se_ve_el_guardado(client, monkeypatch):
    """Paciente sin teléfono cargado hoy: mejor el viejo que ninguno."""
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja")
    _con_telefonos(monkeypatch, {})  # nadie tiene vigente

    res = client.get(f"{BASE}/{apt_id}")
    assert res.json()["patient_phone"] == "011-0000-vieja"


def test_telefono_vigente_vacio_tambien_cae_al_guardado(client, monkeypatch):
    """Un `''` en `users.phone` no es un teléfono: cuenta como "no tiene", no como "borrarlo"."""
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja")
    _con_telefonos(monkeypatch, {5: ""})

    res = client.get(f"{BASE}/{apt_id}")
    assert res.json()["patient_phone"] == "011-0000-vieja"


def test_la_lista_pide_los_telefonos_de_una_sola_vez(client, monkeypatch):
    """Un `IN` por página, no un SELECT por turno: ver dependencies.telefonos_vigentes_de."""
    a = _insert(patient_user_id=5, days_ahead=1)
    b = _insert(patient_user_id=7, days_ahead=2)
    llamadas = _con_telefonos(monkeypatch, {5: "351-1111", 7: "351-2222"})

    res = client.get(f"{BASE}/")
    assert res.status_code == 200
    por_id = {f["appointment_id"]: f["patient_phone"] for f in res.json()["appointments"]}
    assert por_id[a] == "351-1111"
    assert por_id[b] == "351-2222"
    # Una sola llamada para toda la página, con los dos pacientes adentro.
    assert len(llamadas) == 1
    assert llamadas[0] == {5, 7}


def test_crear_turno_responde_con_el_telefono_vigente(client, monkeypatch):
    """
    El teléfono que se manda al crear queda guardado (auditoría), pero la respuesta —igual
    que cualquier lectura— muestra el vigente si `users` tiene uno distinto.
    """
    _con_telefonos(monkeypatch, {5: "351-1111-nueva"})
    start, end = future_slot(days_ahead=10)
    res = client.post(f"{BASE}/", json={
        "dentist_user_id": 1, "patient_user_id": 5, "patient_name": "Paciente Test",
        "patient_phone": "011-0000-la-que-mandó-el-form",
        "start_time_utc": start, "end_time_utc": end,
    })
    assert res.status_code == 200
    assert res.json()["patient_phone"] == "351-1111-nueva"
    assert _fila(res.json()["appointment_id"])["patient_phone"] == "011-0000-la-que-mandó-el-form"


def test_editar_turno_responde_con_el_telefono_vigente(client, monkeypatch):
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja", days_ahead=5)
    _con_telefonos(monkeypatch, {5: "351-1111-nueva"})

    res = client.put(f"{BASE}/{apt_id}", json={"reason": "control"})
    assert res.status_code == 200
    assert res.json()["patient_phone"] == "351-1111-nueva"


def test_cambiar_estado_responde_con_el_telefono_vigente(client, monkeypatch):
    apt_id = _insert(patient_user_id=5, patient_phone="011-0000-vieja", days_ahead=5)
    _con_telefonos(monkeypatch, {5: "351-1111-nueva"})

    res = client.patch(f"{BASE}/{apt_id}/status", json={"status": "CONFIRMED"})
    assert res.status_code == 200
    assert res.json()["patient_phone"] == "351-1111-nueva"
