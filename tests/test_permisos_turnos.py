"""
Permisos y alcance de la agenda, y las ramas que el review encontró sin una sola prueba.

Lo que fija este archivo:

1. **La disponibilidad de un odontólogo no es pública dentro de la clínica.** Sale del
   calendario `primary` de su cuenta Google PERSONAL, así que un paciente leyéndola le ve la
   vida privada. Filtrar por clínica no alcanza: el paciente también tiene `clinic_id`.
2. **Un odontólogo agenda en su propia agenda, no en la de un colega.**
3. **El paciente del turno tiene que ser de la clínica**, igual que el odontólogo.
4. **El widget del dashboard falla cerrado**: un rol desconocido ve lo suyo, no la clínica.
5. **Los filtros por fecha** de la agenda, que es la consulta que más se usa y no tenía test.
6. **No se puede reprogramar al pasado.**
7. **Mover un turno se sincroniza con Google**, que es el 80% de lo que hace la pantalla.
"""
import pytest
from datetime import datetime, timedelta, timezone

from models import Appointment, AppointmentStatus, GcalSyncStatus
from test_agenda_recepcion import _con_google, _db, _fila, _insert

BASE = "/clinic-scheduling-api/v1/appointments"


def _iso(dias=1, hora=10, minutos=0):
    d = datetime.now(timezone.utc) + timedelta(days=dias)
    return datetime(d.year, d.month, d.day, hora, minutos, tzinfo=timezone.utc).isoformat()


def _cuerpo(dentist_user_id=1, patient_user_id=5, dias=1, hora=10, **extra):
    return {
        "dentist_user_id": dentist_user_id,
        "patient_user_id": patient_user_id,
        "patient_name": "Test Patient",
        "start_time_utc": _iso(dias, hora),
        "end_time_utc": _iso(dias, hora + 1),
        **extra,
    }


# ---------------------------------------------------------------------------
# 1. La disponibilidad del odontólogo no la ve cualquiera
# ---------------------------------------------------------------------------

def test_el_paciente_no_puede_leer_la_disponibilidad_del_odontologo(patient_client):
    """
    Los bloques ocupados salen del calendario personal del profesional. Un paciente de la
    clínica leyéndolos es una fuga de su vida privada, no de datos del consultorio.
    """
    res = patient_client.get(
        f"{BASE}/availability/dentist/1",
        params={"start": _iso(1, 8), "end": _iso(1, 20)},
    )
    assert res.status_code == 403, f"un paciente leyó la agenda personal del odontólogo: {res.text}"


def test_un_odontologo_no_espia_la_disponibilidad_de_un_colega(client):
    """
    `client` es el odontólogo 1 (con rol ADMIN además, según conftest): para probar el caso
    del colega hace falta un token de odontólogo puro, que se arma pisando la dependencia.
    """
    import main
    from dependencies import get_roles, get_user_id

    main.app.dependency_overrides[get_roles] = lambda: ["DENTIST"]
    main.app.dependency_overrides[get_user_id] = lambda: 99
    try:
        res = client.get(
            f"{BASE}/availability/dentist/1",
            params={"start": _iso(1, 8), "end": _iso(1, 20)},
        )
        assert res.status_code == 403, f"el odontólogo 99 leyó la agenda del 1: {res.text}"
    finally:
        main.app.dependency_overrides.pop(get_roles, None)
        main.app.dependency_overrides.pop(get_user_id, None)


def test_la_recepcion_si_puede_consultar_la_disponibilidad(receptionist_client):
    """
    El control positivo: agendar para otros es su trabajo. El 400 es porque el odontólogo no
    tiene Google conectado — lo que importa es que NO sea 403.
    """
    res = receptionist_client.get(
        f"{BASE}/availability/dentist/1",
        params={"start": _iso(1, 8), "end": _iso(1, 20)},
    )
    assert res.status_code != 403, "le negó la consulta a la recepción"


# ---------------------------------------------------------------------------
# 2. Un odontólogo agenda sólo en su propia agenda
# ---------------------------------------------------------------------------

def test_un_odontologo_no_puede_agendar_en_la_agenda_de_un_colega(client):
    """
    Sin este freno, el Dr. 99 le ocupaba el hueco al Dr. 1 y le creaba un evento en su Google
    personal — y después ni siquiera podía leer el turno para deshacerlo.
    """
    import main
    from dependencies import get_roles, get_user_id

    main.app.dependency_overrides[get_roles] = lambda: ["DENTIST"]
    main.app.dependency_overrides[get_user_id] = lambda: 99
    try:
        res = client.post(f"{BASE}/", json=_cuerpo(dentist_user_id=1, dias=3))
        assert res.status_code == 403, f"escribió en la agenda del colega: {res.status_code} {res.text}"

        propio = client.post(f"{BASE}/", json=_cuerpo(dentist_user_id=99, dias=3))
        assert propio.status_code == 200, f"no lo dejó agendar en la suya: {propio.text}"
    finally:
        main.app.dependency_overrides.pop(get_roles, None)
        main.app.dependency_overrides.pop(get_user_id, None)


def test_la_recepcion_si_agenda_para_cualquier_odontologo(receptionist_client):
    """El control positivo: administrar la agenda de todos es exactamente su trabajo (#286)."""
    res = receptionist_client.post(f"{BASE}/", json=_cuerpo(dentist_user_id=2, dias=4))
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# 3. El paciente del turno también tiene que ser de la clínica
# ---------------------------------------------------------------------------

def test_no_se_puede_agendar_a_un_paciente_de_otra_clinica(receptionist_client, monkeypatch):
    """
    Era la asimetría del alta: el odontólogo se validaba contra la clínica y el paciente no.
    Un id inventado dejaba un turno con un paciente que no existe.
    """
    import routers.appointments as ra
    monkeypatch.setattr(ra, "_clinic_of_user", lambda uid: 1 if uid != 777 else 2)

    res = receptionist_client.post(f"{BASE}/", json=_cuerpo(patient_user_id=777, dias=5))
    assert res.status_code == 422, f"aceptó un paciente ajeno: {res.status_code} {res.text}"
    assert "Patient" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 4. El widget del dashboard falla cerrado
# ---------------------------------------------------------------------------

def test_upcoming_de_un_rol_desconocido_no_muestra_la_agenda_de_la_clinica(client):
    """
    La cadena de roles no tenía `else`: un token con un rol que este servicio no conoce veía
    los turnos de toda la clínica. El default tiene que ser "sólo lo mío".
    """
    import main
    from dependencies import get_roles, get_user_id

    _insert(dentist_user_id=1, patient_user_id=5, days_ahead=2)
    _insert(dentist_user_id=2, patient_user_id=6, days_ahead=2)

    main.app.dependency_overrides[get_roles] = lambda: ["ALGO_NUEVO"]
    main.app.dependency_overrides[get_user_id] = lambda: 12345
    try:
        res = client.get(f"{BASE}/upcoming")
        assert res.status_code == 200, res.text
        assert res.json()["total"] == 0, f"un rol desconocido vio {res.json()['total']} turnos ajenos"
    finally:
        main.app.dependency_overrides.pop(get_roles, None)
        main.app.dependency_overrides.pop(get_user_id, None)


def test_upcoming_de_un_odontologo_trae_solo_los_suyos(client):
    """La rama del odontólogo nunca se evaluaba: los tests corrían con un usuario que además era ADMIN."""
    import main
    from dependencies import get_roles, get_user_id

    _insert(dentist_user_id=1, days_ahead=2)
    _insert(dentist_user_id=2, days_ahead=2)

    main.app.dependency_overrides[get_roles] = lambda: ["DENTIST"]
    main.app.dependency_overrides[get_user_id] = lambda: 1
    try:
        res = client.get(f"{BASE}/upcoming")
        assert res.status_code == 200, res.text
        dentistas = {t["dentist_user_id"] for t in res.json()["appointments"]}
        assert dentistas == {1}, f"el odontólogo 1 vio turnos de {dentistas}"
    finally:
        main.app.dependency_overrides.pop(get_roles, None)
        main.app.dependency_overrides.pop(get_user_id, None)


def test_upcoming_de_un_paciente_trae_solo_los_suyos(patient_client):
    _insert(patient_user_id=10, days_ahead=2)   # el paciente de `patient_client`
    _insert(patient_user_id=11, days_ahead=2, dentist_user_id=2)

    res = patient_client.get(f"{BASE}/upcoming")
    assert res.status_code == 200, res.text
    pacientes = {t["patient_user_id"] for t in res.json()["appointments"]}
    assert pacientes <= {10}, f"el paciente vio turnos de {pacientes}"


# ---------------------------------------------------------------------------
# 4b. Los dos filtros de la lista tienen alcances distintos, a propósito
# ---------------------------------------------------------------------------

def test_filtrar_por_paciente_le_da_al_odontologo_el_historial_completo(client):
    """
    La solapa Turnos de la historia clínica: el profesional ve TODAS las visitas del paciente
    en la clínica, no sólo las suyas. Una historia clínica a medias es peor que ninguna, y no
    accede a nada nuevo — ya puede abrir la ficha de ese paciente.
    """
    import main
    from dependencies import get_roles, get_user_id

    _insert(dentist_user_id=1, patient_user_id=77, days_ahead=2)
    _insert(dentist_user_id=2, patient_user_id=77, days_ahead=3)

    main.app.dependency_overrides[get_roles] = lambda: ["DENTIST"]
    main.app.dependency_overrides[get_user_id] = lambda: 99
    try:
        con_filtro = client.get(f"{BASE}/", params={"patient_user_id": 77})
        assert con_filtro.status_code == 200, con_filtro.text
        assert con_filtro.json()["total"] == 2, "le faltan visitas del paciente en su ficha"

        # Sin el filtro vuelve a ser SU agenda: el odontólogo 99 no tiene ninguno de esos dos.
        sin_filtro = client.get(f"{BASE}/")
        assert sin_filtro.json()["total"] == 0, "la agenda del odontólogo mostró turnos ajenos"
    finally:
        main.app.dependency_overrides.pop(get_roles, None)
        main.app.dependency_overrides.pop(get_user_id, None)


def test_filtrar_por_profesional_no_le_abre_la_agenda_de_un_colega(client):
    """El otro filtro sí se SUMA al alcance: pedir la agenda de otro no la muestra."""
    import main
    from dependencies import get_roles, get_user_id

    _insert(dentist_user_id=1, days_ahead=2)

    main.app.dependency_overrides[get_roles] = lambda: ["DENTIST"]
    main.app.dependency_overrides[get_user_id] = lambda: 99
    try:
        res = client.get(f"{BASE}/", params={"dentist_user_id": 1})
        assert res.json()["total"] == 0, "vio la agenda del odontólogo 1"
    finally:
        main.app.dependency_overrides.pop(get_roles, None)
        main.app.dependency_overrides.pop(get_user_id, None)


def test_un_paciente_no_puede_espiar_los_turnos_de_otro(patient_client):
    """El filtro por paciente exige rol de staff: para un PACIENTE no hace nada."""
    _insert(patient_user_id=10, days_ahead=2)
    _insert(patient_user_id=11, days_ahead=2)

    res = patient_client.get(f"{BASE}/", params={"patient_user_id": 11})
    assert res.status_code == 200, res.text
    pacientes = {t["patient_user_id"] for t in res.json()["appointments"]}
    assert pacientes <= {10}, f"el paciente 10 vio turnos de {pacientes}"


# ---------------------------------------------------------------------------
# 5. Los filtros por fecha: la consulta de la agenda
# ---------------------------------------------------------------------------

def test_el_rango_de_fechas_acota_la_agenda_y_la_ordena_hacia_adelante(receptionist_client):
    """
    `date_from`/`date_to` es lo que manda el calendario en cada navegación, y no tenía ni un
    test. Con el rango puesto el orden es ascendente (la agenda del día se lee de arriba
    hacia abajo), al revés que el historial.
    """
    _insert(days_ahead=10)
    _insert(days_ahead=20)
    _insert(days_ahead=30)

    desde = (datetime.now() + timedelta(days=15)).isoformat()
    hasta = (datetime.now() + timedelta(days=25)).isoformat()
    res = receptionist_client.get(f"{BASE}/", params={"date_from": desde, "date_to": hasta})

    assert res.status_code == 200, res.text
    turnos = res.json()["appointments"]
    assert len(turnos) == 1, f"el rango trajo {len(turnos)} turnos en vez de 1"

    res_amplio = receptionist_client.get(f"{BASE}/", params={
        "date_from": datetime.now().isoformat(),
        "date_to": (datetime.now() + timedelta(days=40)).isoformat(),
    })
    horas = [t["start_time_utc"] for t in res_amplio.json()["appointments"]]
    assert horas == sorted(horas), f"la agenda no salió en orden ascendente: {horas}"


def test_sin_rango_el_historial_sale_del_mas_nuevo_al_mas_viejo(receptionist_client):
    """El control del test de arriba: sin fechas es el historial, y ése va al revés."""
    _insert(days_ahead=10)
    _insert(days_ahead=20)

    res = receptionist_client.get(f"{BASE}/")
    horas = [t["start_time_utc"] for t in res.json()["appointments"]]
    assert horas == sorted(horas, reverse=True), f"el historial no salió descendente: {horas}"


# ---------------------------------------------------------------------------
# 6. No se reprograma al pasado
# ---------------------------------------------------------------------------

def test_no_se_puede_reprogramar_un_turno_al_pasado(receptionist_client):
    """
    El front lo frena antes, pero la API no puede depender de eso: es una de las reglas del
    epic y no tenía ningún test.
    """
    apt_id = _insert(days_ahead=5)
    ayer = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"start_time_utc": ayer})
    assert res.status_code == 422, f"dejó mandar el turno al pasado: {res.status_code} {res.text}"


# ---------------------------------------------------------------------------
# 7. Mover un turno se sincroniza con Google
# ---------------------------------------------------------------------------

def test_mover_un_turno_actualiza_el_evento_de_google(receptionist_client, monkeypatch):
    """
    La sincronización "de ida" estaba probada para crear y para borrar, pero no para MOVER,
    que es lo que más hace esta pantalla. Sin esto, el odontólogo ve en su celular el horario
    viejo y se presenta a la hora equivocada.
    """
    llamadas = _con_google(monkeypatch, [1])
    apt_id = _insert(dentist_user_id=1, days_ahead=6, google_event_id="evt-viejo")

    nuevo = _iso(7, 15)
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={
        "start_time_utc": nuevo, "end_time_utc": _iso(7, 16),
    })
    assert res.status_code == 200, res.text
    assert ("update",) in llamadas, f"no avisó el cambio a Google: {llamadas}"
    assert _fila(apt_id)["gcal_sync_status"] == GcalSyncStatus.SYNCED


# ---------------------------------------------------------------------------
# 8. Las validaciones de duración que faltaban
# ---------------------------------------------------------------------------

def test_un_turno_que_termina_antes_de_empezar_se_rechaza(receptionist_client):
    res = receptionist_client.post(f"{BASE}/", json={
        **_cuerpo(dias=8), "start_time_utc": _iso(8, 15), "end_time_utc": _iso(8, 14),
    })
    assert res.status_code == 422, res.text


def test_un_turno_de_mas_de_ocho_horas_se_rechaza(receptionist_client):
    """Estirar el borde hasta el infinito bloquearía el día entero del profesional."""
    res = receptionist_client.post(f"{BASE}/", json={
        **_cuerpo(dias=9), "start_time_utc": _iso(9, 8), "end_time_utc": _iso(9, 20),
    })
    assert res.status_code == 422, res.text


@pytest.mark.parametrize("email", ["sin-arroba", "dos@@arrobas.com", "sin@dominio"])
def test_un_mail_mal_escrito_se_rechaza_al_crear(receptionist_client, email):
    """
    Un mail mal tipeado hacía fallar el alta del evento de Google entero: el turno quedaba
    guardado pero sin evento, y el único aviso era un badge.
    """
    res = receptionist_client.post(f"{BASE}/", json=_cuerpo(dias=10, patient_email=email))
    assert res.status_code == 422, f"aceptó el mail {email!r}: {res.text}"


def test_un_mail_bien_escrito_pasa(receptionist_client):
    res = receptionist_client.post(f"{BASE}/", json=_cuerpo(dias=11, patient_email="ana@correo.com"))
    assert res.status_code == 200, res.text
