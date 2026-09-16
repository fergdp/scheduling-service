"""
Integridad de la agenda: que NO se puedan solapar dos turnos del mismo odontólogo.

Salió de la auditoría de concurrencia del epic #294, que encontró que la garantía tenía
agujeros y —peor— que las piezas que sí funcionaban no estaban protegidas por ningún test:
se podían borrar enteras y la suite seguía verde.

Lo que fija este archivo:

1. **La hora que llega con huso se CONVIERTE a UTC, no se le recorta el huso.** Truncarlo
   guarda el reloj de pared: las 10:00 de Buenos Aires quedaban guardadas como las 10:00 UTC,
   o sea tres horas corridas, y dos turnos en el MISMO instante real entraban los dos.
2. **El borde**: dos turnos pegados no se solapan; uno que se pisa por un minuto, sí.
3. **Mover un turno dentro de su propio horario** no choca contra sí mismo.
4. **Correr el inicio conserva la duración**: no se puede estirar un turno sin pedirlo.
5. **El lock pesimista existe de verdad** en la base de producción.
6. **Un evento huérfano en Google no se disfraza de sincronizado.**
"""
import pytest
from datetime import datetime, timedelta, timezone

from models import Appointment, AppointmentStatus, GcalSyncStatus
from test_agenda_recepcion import _con_google, _db, _fila, _insert

BASE = "/clinic-scheduling-api/v1/appointments"

# Buenos Aires es UTC-3 todo el año: un turno a las 10:00 de acá son las 13:00 UTC.
ARGENTINA = timezone(timedelta(hours=-3))


def _crear(cliente, inicio, fin, dentist_user_id=1, patient_user_id=5):
    return cliente.post(f"{BASE}/", json={
        "dentist_user_id": dentist_user_id,
        "patient_user_id": patient_user_id,
        "patient_name": "Test Patient",
        "start_time_utc": inicio.isoformat(),
        "end_time_utc": fin.isoformat(),
        "reason": "Integridad",
    })


def _manana(hora, minuto=0, tz=timezone.utc):
    """Una hora fija de mañana en el huso pedido, para que el turno sea siempre futuro."""
    dia = datetime.now(timezone.utc) + timedelta(days=1)
    return datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=tz)


# ---------------------------------------------------------------------------
# 1. El huso horario no puede abrir un agujero en el chequeo de solapamiento
# ---------------------------------------------------------------------------

def test_la_hora_con_huso_se_guarda_convertida_a_utc(receptionist_client):
    """
    Las 10:00 de Buenos Aires son las 13:00 UTC. Guardar 10:00 deja el turno tres horas
    corrido en la agenda de todo el mundo, y además libera un hueco que está ocupado.
    """
    inicio = _manana(10, tz=ARGENTINA)
    res = _crear(receptionist_client, inicio, inicio + timedelta(minutes=30))
    assert res.status_code == 200, res.json()

    guardado = _fila(res.json()["appointment_id"])["start_time_utc"]
    esperado = inicio.astimezone(timezone.utc).replace(tzinfo=None)
    assert guardado == esperado, (
        f"guardó {guardado} (el reloj de pared) en vez de {esperado} (el instante real)"
    )


def test_dos_turnos_en_el_mismo_instante_con_husos_distintos_chocan(receptionist_client):
    """
    El mismo instante escrito de dos formas: 10:00-03:00 y 13:00Z. El segundo tiene que dar
    409. Si el huso se trunca en vez de convertirse, los dos entran y el odontólogo queda con
    dos pacientes a la misma hora.
    """
    en_argentina = _manana(10, tz=ARGENTINA)
    en_utc = en_argentina.astimezone(timezone.utc)

    primero = _crear(receptionist_client, en_argentina, en_argentina + timedelta(minutes=30))
    assert primero.status_code == 200, primero.json()

    segundo = _crear(receptionist_client, en_utc, en_utc + timedelta(minutes=30))
    assert segundo.status_code == 409, (
        f"aceptó dos turnos en el mismo instante real: {segundo.status_code} {segundo.json()}"
    )


def test_reprogramar_con_huso_tambien_choca(receptionist_client):
    """El mismo agujero por el otro camino: el PUT que reprograma."""
    ocupado = _manana(15)
    apt_ocupa = _crear(receptionist_client, ocupado, ocupado + timedelta(minutes=30))
    assert apt_ocupa.status_code == 200, apt_ocupa.json()

    libre = _manana(9)
    apt_mueve = _crear(receptionist_client, libre, libre + timedelta(minutes=30))
    assert apt_mueve.status_code == 200, apt_mueve.json()

    # 12:00-03:00 son las 15:00 UTC: el horario que ya está ocupado.
    destino = ocupado.astimezone(ARGENTINA)
    res = receptionist_client.put(f"{BASE}/{apt_mueve.json()['appointment_id']}", json={
        "start_time_utc": destino.isoformat(),
        "end_time_utc": (destino + timedelta(minutes=30)).isoformat(),
    })
    assert res.status_code == 409, f"dejó reprogramar sobre un horario ocupado: {res.json()}"


def test_naive_convierte_a_utc_en_vez_de_recortar_el_huso():
    """
    La conversión está en DOS capas —el validador del schema y `_naive` en el router— y cada
    una tapa a la otra, así que romper una sola no se nota en los tests de punta a punta. Por
    eso cada capa lleva además su propio test directo.

    `_naive` es la que usan los filtros `date_from`/`date_to`, que NO pasan por el schema.
    """
    from routers.appointments import _naive

    con_huso = datetime(2030, 5, 6, 10, 0, tzinfo=ARGENTINA)
    assert _naive(con_huso) == datetime(2030, 5, 6, 13, 0), "recortó el huso en vez de convertir"

    sin_huso = datetime(2030, 5, 6, 13, 0)
    assert _naive(sin_huso) == sin_huso, "una fecha sin huso ya viene en UTC: no se toca"


def test_el_validador_del_schema_convierte_a_utc():
    """
    La otra capa: lo que llega por el cuerpo del pedido.

    ⚠️ Se compara el RELOJ (`replace(tzinfo=None)`), no las fechas con huso. Python compara
    dos datetimes con huso por instante, así que `10:00-03:00 == 13:00+00:00` da True y la
    aserción pasaba igual sin la conversión. Lo que importa acá es justamente qué números
    quedan, porque después se guardan tal cual en una columna sin huso.
    """
    from schemas import _a_utc

    convertido = _a_utc(datetime(2030, 5, 6, 10, 0, tzinfo=ARGENTINA))
    assert convertido.utcoffset() == timedelta(0), "quedó con el huso original"
    assert convertido.replace(tzinfo=None) == datetime(2030, 5, 6, 13, 0)

    asumido = _a_utc(datetime(2030, 5, 6, 13, 0))
    assert asumido.utcoffset() == timedelta(0)
    assert asumido.replace(tzinfo=None) == datetime(2030, 5, 6, 13, 0), "sin huso se asume UTC"


# ---------------------------------------------------------------------------
# 2. El borde exacto: pegado no es solapado
# ---------------------------------------------------------------------------

def test_dos_turnos_pegados_no_se_solapan(receptionist_client):
    """Uno termina 15:00 y el otro empieza 15:00: es una agenda normal, no un choque."""
    primero_inicio = _manana(14)
    assert _crear(receptionist_client, primero_inicio,
                  primero_inicio + timedelta(hours=1)).status_code == 200

    segundo = _crear(receptionist_client, primero_inicio + timedelta(hours=1),
                     primero_inicio + timedelta(hours=2))
    assert segundo.status_code == 200, (
        f"rechazó un turno pegado al anterior, que no lo pisa: {segundo.json()}"
    )


def test_pisarse_por_un_minuto_ya_es_solaparse(receptionist_client):
    """El control positivo del test de arriba: un minuto de superposición sí choca."""
    primero_inicio = _manana(14)
    assert _crear(receptionist_client, primero_inicio,
                  primero_inicio + timedelta(hours=1)).status_code == 200

    segundo = _crear(receptionist_client, primero_inicio + timedelta(minutes=59),
                     primero_inicio + timedelta(minutes=89))
    assert segundo.status_code == 409, f"dejó pasar un turno que pisa al anterior: {segundo.json()}"


# ---------------------------------------------------------------------------
# 3. Mover un turno no puede chocar contra sí mismo
# ---------------------------------------------------------------------------

def test_correr_un_turno_media_hora_no_choca_consigo_mismo(receptionist_client):
    """
    Arrastrarlo un poco en la agenda es la acción más común de todas: el horario nuevo se
    superpone con el viejo, y el chequeo tiene que excluir al propio turno.
    """
    inicio = _manana(14)
    apt = _crear(receptionist_client, inicio, inicio + timedelta(hours=1))
    assert apt.status_code == 200, apt.json()

    corrido = inicio + timedelta(minutes=30)
    res = receptionist_client.put(f"{BASE}/{apt.json()['appointment_id']}", json={
        "start_time_utc": corrido.isoformat(),
        "end_time_utc": (corrido + timedelta(hours=1)).isoformat(),
    })
    assert res.status_code == 200, f"el turno chocó contra sí mismo: {res.json()}"


# ---------------------------------------------------------------------------
# 4. Correr el inicio no puede estirar el turno
# ---------------------------------------------------------------------------

def test_adelantar_el_inicio_conserva_la_duracion(receptionist_client):
    """
    Un PUT con sólo `start_time_utc` dejaba el fin viejo: un turno de 30 minutos adelantado
    media hora pasaba a durar una hora y bloqueaba el doble de agenda, sin que nadie lo pida.
    """
    inicio = _manana(14)
    apt = _crear(receptionist_client, inicio, inicio + timedelta(minutes=30))
    assert apt.status_code == 200, apt.json()
    apt_id = apt.json()["appointment_id"]

    adelantado = inicio - timedelta(minutes=30)
    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"start_time_utc": adelantado.isoformat()})
    assert res.status_code == 200, res.json()

    fila = _fila(apt_id)
    duracion = fila["end_time_utc"] - fila["start_time_utc"]
    assert duracion == timedelta(minutes=30), f"el turno pasó a durar {duracion}"


# ---------------------------------------------------------------------------
# 5. El lock pesimista existe en la base de producción
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialecto", ["mysql", "mariadb"])
def test_el_chequeo_de_solapamiento_bloquea_las_filas_en_produccion(dialecto):
    """
    Los tests corren en SQLite, que no soporta `FOR UPDATE`, así que el lock que evita la
    carrera entre dos reservas simultáneas NO se ejecuta nunca en la suite: se puede borrar
    entero y todo queda en verde. Acá se compila la consulta contra el dialecto real y se
    exige que el `FOR UPDATE` esté.

    `mariadb` va aparte porque SQLAlchemy lo reporta como un dialecto distinto de `mysql`:
    cambiar la cadena de conexión apagaría el lock sin que nadie se entere.
    """
    from sqlalchemy.dialects import mysql as dialecto_mysql
    from routers.appointments import _armar_query_solapamiento

    query = _armar_query_solapamiento(
        db=None, dentist_user_id=1, clinic_id=1,
        start=datetime(2030, 1, 1, 10), end=datetime(2030, 1, 1, 11),
        exclude_id=None, dialecto=dialecto,
    )
    sql = str(query.statement.compile(dialect=dialecto_mysql.dialect())).upper()
    assert "FOR UPDATE" in sql, f"sin lock en {dialecto}: la carrera queda abierta"


def test_en_sqlite_no_se_pide_for_update():
    """El control negativo: SQLite no soporta `FOR UPDATE` y pedirlo rompería los tests."""
    from sqlalchemy.dialects import mysql as dialecto_mysql
    from routers.appointments import _armar_query_solapamiento

    query = _armar_query_solapamiento(
        db=None, dentist_user_id=1, clinic_id=1,
        start=datetime(2030, 1, 1, 10), end=datetime(2030, 1, 1, 11),
        exclude_id=None, dialecto="sqlite",
    )
    sql = str(query.statement.compile(dialect=dialecto_mysql.dialect())).upper()
    assert "FOR UPDATE" not in sql


# ---------------------------------------------------------------------------
# 6. Un evento huérfano en Google no puede quedar marcado como sincronizado
# ---------------------------------------------------------------------------

def test_si_no_se_pudo_borrar_el_evento_del_odontologo_anterior_queda_marcado(
        receptionist_client, monkeypatch):
    """
    Al pasar un turno a otro profesional hay que borrar el evento del calendario del anterior
    y crear uno nuevo en el del siguiente. Si el borrado falla, el evento viejo sigue vivo en
    la agenda de Google del primero — y el paciente aparece con dos citas.

    El estado tiene que quedar en FAILED, porque es lo único que ve la recepcionista y lo que
    encuentra la reconciliación. Antes, la creación del evento nuevo lo pisaba con SYNCED y el
    huérfano quedaba invisible.
    """
    _con_google(monkeypatch, [1, 2])
    apt_id = _insert(dentist_user_id=1, days_ahead=3, google_event_id="evt-del-uno")

    import routers.appointments as ra

    def _no_se_puede_borrar(service, cal, event_id):
        raise RuntimeError("Google caído")

    monkeypatch.setattr(ra, "delete_google_event", _no_se_puede_borrar)

    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2})
    assert res.status_code == 200, res.json()

    fila = _fila(apt_id)
    assert fila["dentist_user_id"] == 2, "el cambio de profesional tiene que aplicarse igual"
    assert fila["gcal_sync_status"] == GcalSyncStatus.FAILED, (
        f"el evento huérfano quedó disfrazado de {fila['gcal_sync_status']}"
    )


def test_una_reasignacion_sin_problemas_queda_sincronizada(receptionist_client, monkeypatch):
    """
    El control positivo del test de arriba: si Google responde bien, el turno NO puede quedar
    marcado como fallado — si no, el badge de advertencia aparecería siempre y dejaría de
    significar algo.
    """
    _con_google(monkeypatch, [1, 2])
    apt_id = _insert(dentist_user_id=1, days_ahead=4, google_event_id="evt-del-uno")

    res = receptionist_client.put(f"{BASE}/{apt_id}", json={"dentist_user_id": 2})
    assert res.status_code == 200, res.json()

    fila = _fila(apt_id)
    assert fila["gcal_sync_status"] == GcalSyncStatus.SYNCED
    assert fila["google_event_id"], "tiene que tener el evento nuevo del odontólogo 2"
