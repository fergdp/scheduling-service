"""
El nombre del odontólogo en las respuestas de turnos (#508).

El paciente no puede leer la lista del personal de dental-clinic, así que en «Mis turnos» veía
«Od. #4032» en vez del nombre. Ahora lo trae el turno, leído de `users` en lote.

`conftest.sin_telefonos_vigentes` deja el mapa vacío por defecto (la base de tests no tiene
`users`); acá se lo pisa a mano. La consulta misma se prueba en `test_contacto.py`.
"""
from test_agenda_recepcion import BASE, _insert


def _con_nombres(monkeypatch, mapa):
    """Reemplaza `_nombres_de_odontologos` y devuelve las llamadas que recibió."""
    import routers.appointments as ra
    llamadas = []

    def _fake(user_ids, clinic_id):
        llamadas.append((set(user_ids), clinic_id))
        # Sólo los pedidos, como la consulta: un llamado con los ids equivocados sale sin el nombre.
        return {i: mapa[i] for i in user_ids if i in mapa}

    monkeypatch.setattr(ra, "_nombres_de_odontologos", _fake)
    return llamadas


def test_el_paciente_ve_el_nombre_de_su_odontologo_en_la_lista(patient_client, monkeypatch):
    apt_id = _insert(dentist_user_id=1, patient_user_id=10)
    _con_nombres(monkeypatch, {1: "Laura Aguirre"})

    res = patient_client.get(f"{BASE}/")
    assert res.status_code == 200
    fila = next(a for a in res.json()["appointments"] if a["appointment_id"] == apt_id)
    assert fila["dentist_name"] == "Laura Aguirre"


def test_y_en_el_detalle_con_el_odontologo_del_turno(patient_client, monkeypatch):
    """El detalle comparte el camino con crear, editar y cambiar el estado (`_uno_con_telefono_vigente`)."""
    apt_id = _insert(dentist_user_id=1, patient_user_id=10)
    llamadas = _con_nombres(monkeypatch, {1: "Laura Aguirre"})

    res = patient_client.get(f"{BASE}/{apt_id}")
    assert res.status_code == 200
    assert res.json()["dentist_name"] == "Laura Aguirre"
    assert llamadas == [({1}, 1)]


def test_una_sola_consulta_por_pagina_con_los_odontologos_de_la_clinica(client, monkeypatch):
    _insert(dentist_user_id=1, patient_user_id=5)
    _insert(dentist_user_id=2, patient_user_id=6)
    _insert(dentist_user_id=1, patient_user_id=7)
    llamadas = _con_nombres(monkeypatch, {1: "Laura Aguirre", 2: "Pablo Funes"})

    res = client.get(f"{BASE}/")
    assert res.status_code == 200
    assert llamadas == [({1, 2}, 1)]
    nombres = {a["dentist_user_id"]: a["dentist_name"] for a in res.json()["appointments"]}
    assert nombres == {1: "Laura Aguirre", 2: "Pablo Funes"}


def test_sin_el_nombre_el_turno_sale_igual(patient_client, monkeypatch):
    apt_id = _insert(dentist_user_id=1, patient_user_id=10)
    _con_nombres(monkeypatch, {})

    res = patient_client.get(f"{BASE}/{apt_id}")
    assert res.status_code == 200
    assert res.json()["dentist_name"] is None
