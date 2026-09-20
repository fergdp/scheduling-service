"""
El teléfono vigente del paciente en las respuestas de la lista de espera (issue #314).

Mismo bug que #313 pero en `WaitlistEntry.patient_phone`: se guarda al anotar y nada lo
actualizaba después. `HuecosLiberadosDialog` y `ListaEsperaDialog` usan ese campo para Llamar y
el recordatorio por WhatsApp (#295) — si el paciente cambiaba de número mientras esperaba, esas
dos pantallas seguían yendo al viejo.

`conftest.sin_telefonos_vigentes` deja el mapa vacío por defecto (la base de tests no tiene
`users`), así que el resto de la suite sigue viendo el guardado sin cambios; acá se pisa ese
mapa a mano para probar el reemplazo — mismo mecanismo que `test_telefono_vigente.py`, aplicado
al router de la lista de espera.
"""
from models import WaitlistEntry
from test_agenda_recepcion import BASE, _patch
from test_lista_espera import W, _anotar, _fila, _id_anotado, _turno, _utc


def _con_telefonos(monkeypatch, mapa):
    """Reemplaza `_telefonos_vigentes` en el router de la lista de espera y devuelve las llamadas."""
    import routers.waitlist as rw
    llamadas = []

    def _fake(patient_user_ids):
        llamadas.append(set(patient_user_ids))
        return dict(mapa)

    monkeypatch.setattr(rw, "_telefonos_vigentes", _fake)
    return llamadas


def test_list_muestra_el_telefono_vigente_no_el_guardado(receptionist_client, monkeypatch):
    entry_id = _id_anotado(receptionist_client, 6)
    _con_telefonos(monkeypatch, {6: "351-1111-nueva"})

    res = receptionist_client.get(f"{W}/")
    assert res.status_code == 200
    fila = next(e for e in res.json()["entries"] if e["entry_id"] == entry_id)
    assert fila["patient_phone"] == "351-1111-nueva"
    # El guardado en la base no se toca: es el respaldo si mañana no hay vigente.
    assert _fila(WaitlistEntry, entry_id)["patient_phone"] == "11 5555-0000"


def test_sin_telefono_vigente_se_ve_el_guardado(receptionist_client, monkeypatch):
    """Paciente sin teléfono cargado hoy: mejor el viejo que ninguno."""
    _id_anotado(receptionist_client, 6)
    _con_telefonos(monkeypatch, {})  # nadie tiene vigente

    res = receptionist_client.get(f"{W}/")
    assert res.json()["entries"][0]["patient_phone"] == "11 5555-0000"


def test_telefono_vigente_vacio_tambien_cae_al_guardado(receptionist_client, monkeypatch):
    """Un `''` en `users.phone` no es un teléfono: cuenta como "no tiene", no como "borrarlo"."""
    _id_anotado(receptionist_client, 6)
    _con_telefonos(monkeypatch, {6: ""})

    res = receptionist_client.get(f"{W}/")
    assert res.json()["entries"][0]["patient_phone"] == "11 5555-0000"


def test_anotar_responde_con_el_telefono_vigente(receptionist_client, monkeypatch):
    """El teléfono que se manda al anotar queda guardado, pero la respuesta muestra el vigente."""
    _con_telefonos(monkeypatch, {6: "351-1111-nueva"})

    res = _anotar(receptionist_client, 6)
    assert res.status_code == 200
    assert res.json()["patient_phone"] == "351-1111-nueva"
    assert _fila(WaitlistEntry, res.json()["entry_id"])["patient_phone"] == "11 5555-0000"


def test_editar_entrada_responde_con_el_telefono_vigente(receptionist_client, monkeypatch):
    entry_id = _id_anotado(receptionist_client, 6)
    _con_telefonos(monkeypatch, {6: "351-1111-nueva"})

    res = receptionist_client.patch(f"{W}/{entry_id}", json={"note": "actualizado"})
    assert res.status_code == 200
    assert res.json()["patient_phone"] == "351-1111-nueva"


def test_la_lista_pide_los_telefonos_de_una_sola_vez(receptionist_client, monkeypatch):
    """Un `IN` por página, no un SELECT por entrada: ver dependencies.telefonos_vigentes_de."""
    a = _id_anotado(receptionist_client, 6)
    b = _id_anotado(receptionist_client, 7)
    llamadas = _con_telefonos(monkeypatch, {6: "351-1111", 7: "351-2222"})

    res = receptionist_client.get(f"{W}/")
    assert res.status_code == 200
    por_id = {e["entry_id"]: e["patient_phone"] for e in res.json()["entries"]}
    assert por_id[a] == "351-1111"
    assert por_id[b] == "351-2222"
    assert len(llamadas) == 1
    assert llamadas[0] == {6, 7}


def test_slots_muestra_el_telefono_vigente_de_los_candidatos(receptionist_client, monkeypatch):
    _id_anotado(receptionist_client, 6, dentist=1)
    apt = _turno(_utc(10), dentist=1, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    _con_telefonos(monkeypatch, {6: "351-1111-nueva"})

    res = receptionist_client.get(f"{W}/slots")
    assert res.status_code == 200
    [hueco] = res.json()["slots"]
    [candidato] = hueco["candidates"]
    assert candidato["patient_phone"] == "351-1111-nueva"


def test_los_slots_piden_los_telefonos_de_una_sola_vez_para_todos_los_candidatos(
    receptionist_client, monkeypatch,
):
    """Un `IN` para los candidatos de TODOS los avisos, no uno por hueco."""
    _id_anotado(receptionist_client, 6, dentist=1)
    _id_anotado(receptionist_client, 7, dentist=2)
    apt1 = _turno(_utc(10), dentist=1, paciente=5)
    apt2 = _turno(_utc(11), dentist=2, paciente=8)
    assert _patch(receptionist_client, apt1, "CANCELLED").status_code == 200
    assert _patch(receptionist_client, apt2, "CANCELLED").status_code == 200
    llamadas = _con_telefonos(monkeypatch, {6: "351-1111", 7: "351-2222"})

    res = receptionist_client.get(f"{W}/slots")
    assert res.status_code == 200
    telefonos = {c["entry_id"]: c["patient_phone"] for h in res.json()["slots"] for c in h["candidates"]}
    assert set(telefonos.values()) == {"351-1111", "351-2222"}
    assert len(llamadas) == 1
    assert llamadas[0] == {6, 7}


def test_sin_candidatos_no_pide_telefonos(receptionist_client, monkeypatch):
    """Un aviso sin nadie que lo quiera no dispara ningún `IN` vacío."""
    apt = _turno(_utc(10), dentist=1, paciente=5)
    assert _patch(receptionist_client, apt, "CANCELLED").status_code == 200
    llamadas = _con_telefonos(monkeypatch, {})

    res = receptionist_client.get(f"{W}/slots")
    assert res.json()["slots"] == []
    assert llamadas == [set()]


def test_un_mismo_candidato_en_dos_avisos_a_la_vez_pide_su_telefono_una_sola_vez(
    receptionist_client, monkeypatch,
):
    """
    Quien espera a «cualquier odontólogo» puede ser candidato de dos avisos abiertos al mismo
    tiempo, uno por odontólogo. `telefonos_vigentes_de` dedupea el `IN` (ver test_dependencies.py);
    esto prueba que a nivel router el id repetido no dispara una segunda consulta y que el
    vigente se ve igual en los dos avisos, no sólo en el primero.
    """
    _id_anotado(receptionist_client, 6)  # cualquier odontólogo
    apt1 = _turno(_utc(10), dentist=1, paciente=5)
    apt2 = _turno(_utc(11), dentist=2, paciente=8)
    assert _patch(receptionist_client, apt1, "CANCELLED").status_code == 200
    assert _patch(receptionist_client, apt2, "CANCELLED").status_code == 200
    llamadas = _con_telefonos(monkeypatch, {6: "351-1111-nueva"})

    res = receptionist_client.get(f"{W}/slots")
    assert res.status_code == 200
    huecos = res.json()["slots"]
    assert len(huecos) == 2
    for hueco in huecos:
        [candidato] = hueco["candidates"]
        assert candidato["patient_phone"] == "351-1111-nueva"
    assert len(llamadas) == 1
    assert llamadas[0] == {6}
