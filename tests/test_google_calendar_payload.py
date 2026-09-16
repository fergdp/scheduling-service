"""
Qué se le manda a Google Calendar, exactamente.

Estas tres funciones eran las únicas del camino de turnos sin ejecutar en ningún test: todos
los demás las reemplazan por un mock en el punto de importación, así que el CUERPO del pedido
—el formato de la fecha, a quién se invita y si se manda mail— nunca se verificaba.

Importa porque un error acá no se ve en el sistema: el turno se guarda igual y lo único que
queda es un badge de sincronización fallida, o peor, un evento con la hora corrida en el
celular del odontólogo.
"""
import pytest
from datetime import datetime

from utils.google_calendar import (
    create_google_event, delete_google_event, get_free_busy, update_google_event,
)

INICIO = datetime(2026, 9, 16, 13, 0)   # naive UTC, como lo guarda la base
FIN = datetime(2026, 9, 16, 13, 30)


class _Llamada:
    """Guarda con qué argumentos se llamó y qué devuelve `.execute()`."""

    def __init__(self, devuelve=None):
        self.kwargs = None
        self._devuelve = devuelve or {}

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        return self._devuelve


class _ServicioFalso:
    """Lo mínimo que usa el código: `service.events().insert(...).execute()`."""

    def __init__(self, id_evento="evt-123"):
        self.insert = _Llamada({"id": id_evento})
        self.update = _Llamada()
        self.delete = _Llamada()
        self.freebusy_query = _Llamada()

    def events(self):
        return self

    def freebusy(self):
        return _FreebusyFalso(self.freebusy_query)


class _FreebusyFalso:
    def __init__(self, llamada):
        self._llamada = llamada

    def query(self, **kwargs):
        return self._llamada(**kwargs)


# ---------------------------------------------------------------------------
# Crear
# ---------------------------------------------------------------------------

def test_crear_manda_la_hora_en_utc_y_devuelve_el_id():
    """
    La base guarda UTC naive y Google necesita saber el huso. Si se manda sin la `Z`, Google
    interpreta la hora como local del calendario y el turno aparece corrido en el celular.
    """
    servicio = _ServicioFalso("evt-abc")

    id_evento = create_google_event(servicio, "primary", INICIO, FIN, "Turno", "Detalle")

    assert id_evento == "evt-abc"
    cuerpo = servicio.insert.kwargs["body"]
    assert cuerpo["start"] == {"dateTime": "2026-09-16T13:00:00Z", "timeZone": "UTC"}
    assert cuerpo["end"] == {"dateTime": "2026-09-16T13:30:00Z", "timeZone": "UTC"}
    assert cuerpo["summary"] == "Turno"


def test_crear_sin_invitados_no_manda_mail():
    """
    `sendUpdates` decide si Google le escribe a alguien. Sin invitados tiene que ser "none":
    con "all" Google manda mails de un evento que no tiene destinatarios.
    """
    servicio = _ServicioFalso()
    create_google_event(servicio, "primary", INICIO, FIN, "Turno", "Detalle", [])

    assert servicio.insert.kwargs["sendUpdates"] == "none"
    assert "attendees" not in servicio.insert.kwargs["body"]


def test_crear_con_el_paciente_lo_invita_y_le_avisa():
    servicio = _ServicioFalso()
    create_google_event(servicio, "primary", INICIO, FIN, "Turno", "Detalle", ["ana@correo.com"])

    assert servicio.insert.kwargs["sendUpdates"] == "all"
    assert servicio.insert.kwargs["body"]["attendees"] == [{"email": "ana@correo.com"}]


# ---------------------------------------------------------------------------
# Mover y borrar
# ---------------------------------------------------------------------------

def test_mover_actualiza_el_evento_con_el_horario_nuevo():
    servicio = _ServicioFalso()
    update_google_event(servicio, "primary", "evt-1", "Turno", INICIO, FIN, "Detalle")

    assert servicio.update.kwargs["eventId"] == "evt-1"
    assert servicio.update.kwargs["body"]["start"]["dateTime"] == "2026-09-16T13:00:00Z"
    assert servicio.update.kwargs["sendUpdates"] == "none"


def test_mover_con_invitado_le_avisa_del_cambio():
    """Si el paciente está invitado, tiene que enterarse de que le movieron el turno."""
    servicio = _ServicioFalso()
    update_google_event(servicio, "primary", "evt-1", "Turno", INICIO, FIN, "Detalle",
                        ["ana@correo.com"])

    assert servicio.update.kwargs["sendUpdates"] == "all"
    assert servicio.update.kwargs["body"]["attendees"] == [{"email": "ana@correo.com"}]


def test_borrar_siempre_avisa():
    """Cancelar un turno sí o sí tiene que avisarle al paciente que tenía la invitación."""
    servicio = _ServicioFalso()
    delete_google_event(servicio, "primary", "evt-1")

    assert servicio.delete.kwargs == {
        "calendarId": "primary", "eventId": "evt-1", "sendUpdates": "all",
    }


# ---------------------------------------------------------------------------
# Consultar ocupación
# ---------------------------------------------------------------------------

def test_los_bloques_ocupados_salen_de_la_respuesta():
    servicio = _ServicioFalso()
    servicio.freebusy_query = _Llamada({
        "calendars": {"primary": {"busy": [{"start": "2026-09-16T13:00:00Z",
                                            "end": "2026-09-16T13:30:00Z"}]}}
    })

    ocupado = get_free_busy(servicio, "primary", INICIO, FIN)

    assert ocupado == [{"start": "2026-09-16T13:00:00Z", "end": "2026-09-16T13:30:00Z"}]


def test_si_google_reporta_un_error_se_falla_ruidoso():
    """
    Google devuelve 200 con un `errors` adentro cuando no puede leer el calendario. Devolver
    una lista vacía ahí mostraría huecos libres que en realidad están ocupados, y el mostrador
    agendaría encima.
    """
    servicio = _ServicioFalso()
    servicio.freebusy_query = _Llamada({
        "calendars": {"primary": {"errors": [{"domain": "global", "reason": "notFound"}]}}
    })

    with pytest.raises(RuntimeError, match="freebusy errors"):
        get_free_busy(servicio, "primary", INICIO, FIN)
