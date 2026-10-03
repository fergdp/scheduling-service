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
7. **La cota de abajo del chequeo de solapamiento (#309) no excluye un choque real.**
8. **El tope de 8 h lo hace cumplir la base, no sólo Python (#319, #321)** — y `models.py` dice lo
   mismo que las migraciones, y ninguna lo saca.
9. **Las claves foráneas de las migraciones tienen el tipo de la columna que referencian (#344)**,
   que es lo que exige MySQL y SQLite no mira.
"""
import functools
import os
import re
import subprocess
import sys
from pathlib import Path

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
# 7. La cota de abajo del chequeo de solapamiento (#309): acota el rango sin
#    cambiar el resultado.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialecto", ["mysql", "sqlite"])
def test_la_cota_de_abajo_pide_exactamente_8_horas_antes(dialecto):
    """
    `start_time_utc >= start - 8h` tiene que estar en la consulta para CUALQUIER dialecto —a
    diferencia del `FOR UPDATE`, esto no es un lock, es un filtro que acota el índice—, y con
    el valor exacto: un turno dura como mucho 8 horas (`_validate_times`), así que 7h59 dejaría
    afuera un choque real y 8h01 dejaría de acotar nada.
    """
    from sqlalchemy.dialects import mysql as dialecto_mysql
    from routers.appointments import _armar_query_solapamiento

    inicio = datetime(2030, 6, 15, 14, 0)
    query = _armar_query_solapamiento(
        db=None, dentist_user_id=1, clinic_id=1,
        start=inicio, end=inicio + timedelta(minutes=30),
        exclude_id=None, dialecto=dialecto,
    )
    sql = str(query.statement.compile(
        dialect=dialecto_mysql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    cota_correcta = (inicio - timedelta(hours=8)).isoformat(sep=" ")
    assert cota_correcta in sql, f"no encontré la cota {cota_correcta!r} en:\n{sql}"


def test_la_cota_de_abajo_no_excluye_un_choque_pegado_al_borde(receptionist_client):
    """
    Control positivo del #309 contra la base real: un turno que arranca 7h59 antes del nuevo y
    dura las 8 horas máximas todavía lo pisa por un minuto. Si la cota fuera de 8h exactas mal
    aplicada (`>` en vez de `>=`) o de menos de 8h, este choque real se dejaría de ver.
    """
    nuevo_inicio = _manana(14)
    viejo_inicio = nuevo_inicio - timedelta(hours=7, minutes=59)
    viejo = _crear(receptionist_client, viejo_inicio, viejo_inicio + timedelta(hours=8))
    assert viejo.status_code == 200, viejo.json()

    nuevo = _crear(receptionist_client, nuevo_inicio, nuevo_inicio + timedelta(minutes=30))
    assert nuevo.status_code == 409, (
        f"la cota de abajo tapó un choque real: {nuevo.status_code} {nuevo.json()}"
    )


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


# ---------------------------------------------------------------------------
# 8. El tope de 8 h también lo hace cumplir la base (#319, #321)
# ---------------------------------------------------------------------------

CHECK_DURACION = "chk_appointments_max_duration"
RAIZ = Path(__file__).resolve().parent.parent

# La tabla, con o sin esquema y con o sin comillas invertidas (las sentencias ya vienen con un
# solo espacio entre palabras).
_TABLA = r"(?:`?\w+`?\.)?`?appointments`?(?![\w`])"
# Una sentencia que se lleva la tabla, y con ella el CHECK: borrarla o renombrarla, sola o en una
# lista, en mayúsculas o en minúsculas. `RENAME COLUMN`, `RENAME INDEX` y `RENAME KEY` son otra cosa.
_SE_VA_LA_TABLA = re.compile(
    rf"^drop table (?:if exists )?(?:.*, ?)?{_TABLA}"
    rf"|^rename table (?:.*, ?)?{_TABLA} to\b"
    rf"|^alter table {_TABLA} (?:.*, ?)?rename (?!column\b|index\b|key\b)",
    re.IGNORECASE,
)
_SE_AGREGA_EL_CHECK = re.compile(
    rf"alter table {_TABLA} add constraint `?{CHECK_DURACION}`? check \((.+)\)", re.IGNORECASE,
)


def _turno_que_dura(duracion):
    """
    Un turno insertado DIRECTO en la base, sin pasar por `_validate_times`: es justo lo que el CHECK
    tiene que frenar — una migración, un script a mano, un endpoint nuevo que se olvide de validar.
    """
    inicio = datetime(2030, 1, 7, 9, 0)
    return Appointment(
        clinic_id=1, dentist_user_id=1, patient_user_id=5, patient_name="Paciente Test",
        start_time_utc=inicio, end_time_utc=inicio + duracion,
    )


def test_la_base_rechaza_un_turno_de_mas_de_8_horas():
    """
    Antes del #321 el CHECK existía sólo en la migración de MySQL: la suite (SQLite) no lo creaba,
    así que borrarlo o romperlo no ponía ningún test en rojo. Ahora lo declara `models.py`, y SQLite
    lo hace cumplir.
    """
    from sqlalchemy.exc import IntegrityError
    db = _db()
    try:
        # Un segundo de más: el borde exacto, para que no sobreviva un tope corrido unos segundos.
        db.add(_turno_que_dura(timedelta(hours=8, seconds=1)))
        with pytest.raises(IntegrityError, match=CHECK_DURACION):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_la_base_acepta_un_turno_de_exactamente_8_horas():
    """El borde es inclusivo: 8 h justas entran (`<=`, igual que la migración)."""
    db = _db()
    try:
        db.add(_turno_que_dura(timedelta(hours=8)))
        db.commit()
    finally:
        db.close()


@functools.lru_cache(maxsize=None)
def _sql_de_las_migraciones():
    """
    El SQL que corre `alembic upgrade head` contra MySQL, sentencia por sentencia y sin los
    comentarios, generado sin conectarse a ninguna base (modo offline, `--sql`).

    Se mira el SQL y no el código de las migraciones: da igual CÓMO una migración nueva tocara el
    CHECK —`drop_constraint`, SQL crudo, el nombre armado en un f-string o en un helper, la tabla
    recreada—, en el SQL sale igual. De paso queda fijado que las migraciones se generan offline,
    algo que ya se cuidaba (`7b3e9c1f5a2d` no le pregunta a la base en ese modo).

    En otro proceso, porque `env.py` llama a `load_dotenv()`: en éste dejaría las variables del
    `.env` cargadas para el resto de la suite.
    """
    salida = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=RAIZ, capture_output=True, encoding="utf-8", timeout=120,
        env={**os.environ, "DATABASE_URL": "mysql+pymysql://offline@localhost/offline", "PYTHONUTF8": "1"},
    )
    assert salida.returncode == 0, (
        "`alembic upgrade head --sql` (sin base) falló. Si una migración nueva le pregunta algo a la"
        " base, que lo saltee con `context.is_offline_mode()`, como `7b3e9c1f5a2d`:\n" + salida.stderr
    )
    lineas = [linea for linea in salida.stdout.splitlines() if not linea.startswith("--")]
    return tuple(" ".join(s.split()) for s in " ".join(lineas).split(";") if s.strip())


def _check_que_queda(sentencias):
    """
    La expresión del CHECK de duración después de la última sentencia, o `None` si no queda.

    Cualquier sentencia que lo nombre y no sea agregarlo cuenta como que lo saca: si algún día se lo
    cambia a propósito, este test se cambia a propósito.
    """
    expresion = None
    for sentencia in sentencias:
        if _SE_VA_LA_TABLA.match(sentencia):
            expresion = None
        elif CHECK_DURACION in sentencia.lower():
            agregado = _SE_AGREGA_EL_CHECK.fullmatch(sentencia)
            expresion = agregado.group(1) if agregado else None
    return expresion


def test_las_migraciones_dejan_puesto_el_tope_de_duracion():
    """
    Si una migración nueva lo saca por error, el tope deja de valer en producción y la cota de abajo
    del chequeo de solapamiento (#309, que asume turnos de 8 h como mucho) puede volver a dejar
    choques afuera sin que ningún otro test lo note.
    """
    assert _check_que_queda(_sql_de_las_migraciones()) is not None


@pytest.mark.parametrize("despues", [
    "ALTER TABLE appointments DROP CHECK chk_appointments_max_duration",       # drop_constraint en MySQL
    "ALTER TABLE appointments DROP CONSTRAINT chk_appointments_max_duration",  # en MariaDB, o a mano
    "alter table appointments drop check chk_appointments_max_duration",
    "ALTER TABLE appointments ALTER CHECK chk_appointments_max_duration NOT ENFORCED",
    "DROP TABLE appointments",
    "drop table appointments",
    "DROP TABLE dental_scheduling_db.appointments",
    "DROP TABLE appointments_viejos, appointments",
    "ALTER TABLE appointments RENAME turnos",                                  # rename_table
    "alter table appointments rename to turnos",
    "ALTER TABLE appointments ADD COLUMN x INT, RENAME TO turnos",
    "RENAME TABLE appointments TO turnos",
    "rename table appointments to turnos",
    "RENAME TABLE otra TO otra2, appointments TO turnos",
])
def test_el_guard_ve_que_una_migracion_lo_saca(despues):
    """
    Control positivo: las mismas sentencias, con una más al final que saca el CHECK. Sin esto, una
    forma de sacarlo que el parseo no reconociera dejaría el test de arriba en verde con el CHECK ya
    ido.
    """
    assert _check_que_queda(_sql_de_las_migraciones() + (despues,)) is None


@pytest.mark.parametrize("despues", [
    "ALTER TABLE appointments RENAME COLUMN reason TO motivo",
    "ALTER TABLE appointments ADD COLUMN sala INTEGER",
    "DROP TABLE appointments_viejos",
    "RENAME TABLE appointments_nuevos TO appointments_viejos",
])
def test_el_guard_no_confunde_otra_sentencia_con_sacarlo(despues):
    """Control negativo: tocar la tabla, o borrar otra que empieza igual, no saca el CHECK."""
    assert _check_que_queda(_sql_de_las_migraciones() + (despues,)) is not None


@pytest.mark.parametrize("url_dialecto", ["mysql+pymysql://", "mariadb+pymysql://"])
def test_el_check_de_duracion_es_el_de_la_migracion(url_dialecto):
    """
    `models.py` y las migraciones tienen que decir LO MISMO: si alguien cambia uno sin el otro, una
    base armada con `create_all()` y una armada con Alembic tendrían reglas distintas. Se compila la
    tabla contra el dialecto real y se compara con el CHECK que dejan las migraciones.

    `mariadb` aparte: SQLAlchemy lo reporta como otro dialecto, y sin su versión el CHECK no compila.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateTable

    expresion = _check_que_queda(_sql_de_las_migraciones())
    dialecto = make_url(url_dialecto).get_dialect()()
    ddl = " ".join(str(CreateTable(Appointment.__table__).compile(dialect=dialecto)).split())
    assert f"CONSTRAINT {CHECK_DURACION} CHECK ({expresion})" in ddl, ddl


def test_el_check_en_otra_base_avisa_donde_agregarlo():
    """Con una base para la que el CHECK no tiene versión, armar la tabla falla diciendo dónde
    agregarla, en vez de crearla sin el tope."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    with pytest.raises(NotImplementedError, match="agregarla en models.py"):
        CreateTable(Appointment.__table__).compile(dialect=postgresql.dialect())


# ---------------------------------------------------------------------------
# 9. Las claves foráneas de las migraciones tienen el tipo de lo que referencian (#344)
# ---------------------------------------------------------------------------

# `nombre TIPO`. El largo o el ancho entre paréntesis no se guarda —MySQL no lo compara en una clave
# foránea—; el `UNSIGNED` sí: `BIGINT` y `BIGINT UNSIGNED` no son lo mismo.
_COLUMNA = r"`?(\w+)`? (\w+)(?:\([^)]*\))?( unsigned)?"
# Con el nombre de índice que MySQL deja poner entre `FOREIGN KEY` y el paréntesis.
_CLAVE_FORANEA = r"foreign key(?: `?\w+`?)? ?\(`?(\w+)`?\) references `?(\w+)`? ?\(`?(\w+)`?\)"
# Lo que en un `CREATE TABLE` o un `ALTER TABLE` empieza con una palabra y no es una columna.
_NO_ES_COLUMNA = r"(primary key|unique|constraint|check|index|key|foreign|drop|rename|alter)\b"


def _tipo(columna):
    """El tipo como lo compara MySQL en una clave foránea: `INT` es `INTEGER`."""
    base = columna.group(2).upper()
    return ("INTEGER" if base == "INT" else base) + (columna.group(3) or "").upper()


def _partes(cuerpo):
    """Un `CREATE TABLE` o un `ALTER TABLE`, cortado en las comas que no están entre paréntesis."""
    partes, nivel, actual = [], 0, ""
    for caracter in cuerpo:
        if caracter == "," and nivel == 0:
            partes.append(actual.strip())
            actual = ""
            continue
        nivel += (caracter == "(") - (caracter == ")")
        actual += caracter
    return partes + [actual.strip()]


def _claves_foraneas_de_otro_tipo(sentencias):
    """
    Las claves foráneas del SQL cuya columna no tiene el tipo de la que referencia, como texto.

    Lee las columnas y las claves de los `CREATE TABLE` y de los `ALTER TABLE ... ADD | MODIFY |
    CHANGE`. Lo que no sabe leer —una clave compuesta, una columna que no encuentra— lo dice
    fallando, no lo saltea. No sigue las claves que una migración saca después: las compara igual.
    """
    tipos, claves = {}, []
    for sentencia in sentencias:
        creada = re.match(r"create table `?(\w+)`? \((.*)\)", sentencia, re.IGNORECASE)
        alterada = re.match(r"alter table `?(\w+)`? (.*)", sentencia, re.IGNORECASE)
        if not (creada or alterada):
            continue
        tabla, cuerpo = (creada or alterada).groups()
        for parte in _partes(cuerpo):
            parte = re.sub(r"^(add|modify)( column)? ", "", parte, flags=re.IGNORECASE)
            # `CHANGE vieja nueva TIPO`: el tipo nuevo vale también para el nombre viejo, que es el
            # que guardan las claves ya leídas.
            cambiada = re.match(r"change(?: column)? `?(\w+)`? ", parte, re.IGNORECASE)
            if cambiada:
                parte = parte[cambiada.end():]
            clave = re.search(_CLAVE_FORANEA, parte, re.IGNORECASE)
            columna = re.match(_COLUMNA, parte, re.IGNORECASE)
            if clave:
                claves.append((tabla, *clave.groups()))
            elif columna and not re.match(_NO_ES_COLUMNA, parte, re.IGNORECASE):
                tipos[tabla, columna.group(1)] = _tipo(columna)
                if cambiada:
                    tipos[tabla, cambiada.group(1)] = _tipo(columna)

    declaradas = sum(len(re.findall(r"(?<!drop )foreign key\b", s, re.IGNORECASE)) for s in sentencias)
    assert len(claves) == declaradas, f"el SQL declara {declaradas} claves foráneas y se leyeron {len(claves)}"
    sin_tipo = [clave for clave in claves if clave[:2] not in tipos or clave[2:] not in tipos]
    assert not sin_tipo, f"no se encontró el tipo de alguna columna de estas claves: {sin_tipo}"
    return [
        f"{tabla}.{columna} {tipos[tabla, columna]} -> {otra}.{referida} {tipos[otra, referida]}"
        for tabla, columna, otra, referida in claves
        if tipos[tabla, columna] != tipos[otra, referida]
    ]


def test_las_claves_foraneas_de_las_migraciones_tienen_el_tipo_de_lo_que_referencian():
    """
    MySQL exige que la columna de una clave foránea tenga exactamente el tipo de la referenciada
    (error 3780) y SQLite no, así que el resto de la suite no lo ve. Pasó con `7b3e9c1f5a2d`: en
    modo offline armaba las dos claves a `appointments` con `INTEGER`, contra un `BIGINT` (#344).
    """
    assert _claves_foraneas_de_otro_tipo(_sql_de_las_migraciones()) == []


@pytest.mark.parametrize("de_otro_tipo, despues", [
    # Una tabla más, con la clave a `appointments` en `INTEGER`: armada en el `CREATE TABLE`...
    ("notas.appointment_id INTEGER", (
        "CREATE TABLE notas ( nota_id INTEGER NOT NULL AUTO_INCREMENT, appointment_id INTEGER NOT NULL,"
        " PRIMARY KEY (nota_id), FOREIGN KEY(appointment_id) REFERENCES appointments (appointment_id) )",
    )),
    # ... sumada después con `ALTER TABLE`...
    ("notas.appointment_id INTEGER", (
        "CREATE TABLE notas ( nota_id INTEGER NOT NULL AUTO_INCREMENT, PRIMARY KEY (nota_id) )",
        "ALTER TABLE notas ADD COLUMN appointment_id INTEGER NOT NULL",
        "ALTER TABLE notas ADD CONSTRAINT fk_notas_turno FOREIGN KEY(appointment_id)"
        " REFERENCES appointments (appointment_id)",
    )),
    # ... o con nombre de índice.
    ("notas.appointment_id INTEGER", (
        "CREATE TABLE notas ( nota_id INTEGER NOT NULL AUTO_INCREMENT, appointment_id INTEGER, PRIMARY KEY"
        " (nota_id), FOREIGN KEY ix_turno (appointment_id) REFERENCES appointments (appointment_id) )",
    )),
    # Lo único distinto es el signo.
    ("notas.appointment_id BIGINT UNSIGNED", (
        "CREATE TABLE notas ( nota_id INTEGER NOT NULL AUTO_INCREMENT, appointment_id BIGINT UNSIGNED,"
        " PRIMARY KEY (nota_id), FOREIGN KEY(appointment_id) REFERENCES appointments (appointment_id) )",
    )),
    # Una clave que estaba bien, y una migración posterior le cambia el tipo a la columna: lo que
    # genera `op.alter_column` (con y sin nombre nuevo) y lo mismo escrito a mano.
    ("waitlist_entries.appointment_id INTEGER", (
        "ALTER TABLE waitlist_entries CHANGE appointment_id turno_id INTEGER NULL",
    )),
    ("waitlist_entries.appointment_id INTEGER", (
        "ALTER TABLE waitlist_entries MODIFY appointment_id INTEGER NULL",
    )),
    ("waitlist_entries.appointment_id INTEGER", (
        "alter table waitlist_entries change column appointment_id appointment_id int null",
    )),
])
def test_el_guard_ve_una_clave_foranea_de_otro_tipo(de_otro_tipo, despues):
    """Control positivo: las mismas sentencias, más algo que deja una clave a `appointments` con un
    tipo que no es el suyo."""
    assert _claves_foraneas_de_otro_tipo(_sql_de_las_migraciones() + despues) == [
        f"{de_otro_tipo} -> appointments.appointment_id BIGINT",
    ]


@pytest.mark.parametrize("despues", [
    # El ancho no cuenta.
    ("CREATE TABLE notas ( nota_id INTEGER NOT NULL, appointment_id BIGINT(20), PRIMARY KEY (nota_id),"
     " FOREIGN KEY(appointment_id) REFERENCES appointments (appointment_id) )",),
    # `INT` es `INTEGER`, y el largo de un texto tampoco cuenta.
    ("CREATE TABLE salas ( sala_id INT NOT NULL, codigo VARCHAR(36) NOT NULL, PRIMARY KEY (sala_id) )",
     "CREATE TABLE reservas ( sala_id INTEGER, codigo VARCHAR(50), FOREIGN KEY(sala_id) REFERENCES salas"
     " (sala_id), FOREIGN KEY(codigo) REFERENCES salas (codigo) )"),
    # Sacar una clave no descuadra la cuenta de las que hay que leer.
    ("ALTER TABLE waitlist_entries DROP FOREIGN KEY waitlist_entries_ibfk_1",),
])
def test_el_guard_no_marca_una_clave_foranea_que_mysql_acepta(despues):
    """Control negativo: escrituras distintas del mismo tipo no son un tipo distinto."""
    assert _claves_foraneas_de_otro_tipo(_sql_de_las_migraciones() + despues) == []


@pytest.mark.parametrize("mensaje, despues", [
    ("se leyeron", ("CREATE TABLE pares ( a BIGINT, b BIGINT, FOREIGN KEY(a, b) REFERENCES otra (x, y) )",)),
    ("no se encontró el tipo", ("CREATE TABLE sueltas ( x BIGINT, FOREIGN KEY(x) REFERENCES no_existe (id) )",)),
])
def test_el_guard_no_saltea_una_clave_foranea_que_no_sabe_leer(mensaje, despues):
    """Una clave compuesta, o a una tabla que el SQL no crea: tiene que fallar, no pasar de largo."""
    with pytest.raises(AssertionError, match=mensaje):
        _claves_foraneas_de_otro_tipo(_sql_de_las_migraciones() + despues)
