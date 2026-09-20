import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import datetime, timedelta, timezone
from typing import Optional
from models import AppointmentStatus, FreedSlotReason, GcalSyncStatus, WaitlistStatus

# Forma mínima de un mail: algo, arroba, dominio con punto. No valida que exista.
_FORMA_DE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _a_utc(v: datetime) -> datetime:
    """
    Lleva la fecha a UTC. Sin huso se asume que ya viene en UTC (los campos se llaman
    `*_time_utc`); con huso se CONVIERTE.

    ⚠️ Antes se le pegaba el huso UTC sin convertir, así que el instante que validaba el
    "tiene que ser futuro" no era el instante que después se guardaba. Con eso, un
    `2026-09-15T14:43-14:00` pasaba el validador (su instante real es futuro) y terminaba
    guardado diez horas en el pasado. Lo fija `tests/test_integridad_turnos.py`.
    """
    try:
        return v.astimezone(timezone.utc) if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    except OverflowError:
        # «9999-12-31T23:59:59-14:00» no entra en un datetime al pasarlo a UTC. Sin esto saltaba
        # un OverflowError que Pydantic no convierte: 500 y un traceback en el log por pedido.
        raise ValueError("date is out of range")


class OAuthUrlResponse(BaseModel):
    auth_url: str


class GoogleConfigBase(BaseModel):
    sync_enabled: bool = True
    default_timezone: str = "UTC"
    default_appointment_duration_minutes: int = Field(default=30, ge=15, le=480)


class GoogleConfigResponse(GoogleConfigBase):
    config_id: int
    dentist_user_id: int
    clinic_id: int
    google_email: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GoogleConfigUpdate(BaseModel):
    sync_enabled: Optional[bool] = None
    default_timezone: Optional[str] = None
    default_appointment_duration_minutes: Optional[int] = Field(default=None, ge=15, le=480)


class AppointmentBase(BaseModel):
    dentist_user_id: int
    start_time_utc: datetime
    end_time_utc: datetime
    patient_timezone: str = "UTC"
    reason: Optional[str] = None


class AppointmentCreate(BaseModel):
    dentist_user_id: int = Field(gt=0)
    patient_user_id: int = Field(gt=0)
    # Los topes son los de las columnas. Sin ellos, un dato más largo —un teléfono con dos números y
    # una aclaración, que la ficha del paciente acepta— llegaba a MySQL y el alta daba 500 en vez
    # de un 422 que se pueda explicar (salió con «Darle el turno» desde la lista de espera, #298).
    patient_name: Optional[str] = Field(default=None, max_length=255)
    patient_email: Optional[str] = Field(default=None, max_length=255)
    patient_phone: Optional[str] = Field(default=None, max_length=50)
    patient_dni: Optional[str] = Field(default=None, max_length=50)
    patient_address: Optional[str] = Field(default=None, max_length=255)
    start_time_utc: datetime
    end_time_utc: Optional[datetime] = None  # si no se envía, usa la duración default del odontólogo
    patient_timezone: str = Field(default="UTC", max_length=50)
    reason: Optional[str] = None
    # Lista de espera (#298): el turno se le da a ese paciente de la lista, que sale de la lista
    # en el mismo commit. Opcional: sin esto el alta es la de siempre.
    waitlist_entry_id: Optional[int] = Field(default=None, gt=0)

    @field_validator("start_time_utc")
    @classmethod
    def start_must_be_future(cls, v: datetime) -> datetime:
        if _a_utc(v) <= datetime.now(timezone.utc):
            raise ValueError("start_time_utc must be in the future")
        return _a_utc(v)

    @field_validator("reason")
    @classmethod
    def reason_max_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > 500:
            raise ValueError("reason must be 500 characters or fewer")
        return v

    @field_validator("patient_email")
    @classmethod
    def email_con_forma_de_email(cls, v: Optional[str]) -> Optional[str]:
        """
        El mail viaja como invitado a Google Calendar. Uno mal tipeado hace fallar el alta del
        evento ENTERO: el turno queda guardado pero sin evento en la agenda del odontólogo, y
        el único aviso es un badge de sincronización fallida. Mejor rechazarlo acá.

        Es una comprobación de forma, no de existencia: el validador estricto pediría la
        dependencia `email-validator`, que este servicio no tiene instalada.
        """
        if v is None or v == "":
            return None
        if not _FORMA_DE_EMAIL.match(v):
            raise ValueError("patient_email is not a valid email address")
        return v

    @field_validator("end_time_utc")
    @classmethod
    def normalize_end_time(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _a_utc(v) if v is not None else None


class AppointmentUpdate(BaseModel):
    """
    Editar fecha/hora, motivo y/o odontólogo de un turno activo.
    dentist_user_id: cambiar de profesional (sólo staff). El turno se saca del Google
    Calendar del odontólogo anterior y se crea en el del nuevo, si lo tiene conectado.
    """
    start_time_utc: Optional[datetime] = None
    end_time_utc: Optional[datetime] = None
    reason: Optional[str] = None
    dentist_user_id: Optional[int] = Field(default=None, gt=0)
    # Lista de espera (#298): «Adelantar su turno». El paciente del turno sale de la lista en el
    # mismo commit en que su turno se mueve al hueco.
    waitlist_entry_id: Optional[int] = Field(default=None, gt=0)

    @field_validator("start_time_utc")
    @classmethod
    def start_must_be_future(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        if _a_utc(v) <= datetime.now(timezone.utc):
            raise ValueError("start_time_utc must be in the future")
        return _a_utc(v)

    @field_validator("end_time_utc")
    @classmethod
    def normalize_end_time(cls, v: Optional[datetime]) -> Optional[datetime]:
        """El fin no tenia validador: un `+05:00` entraba crudo y corria el turno cinco horas."""
        return _a_utc(v) if v is not None else None

    @field_validator("reason")
    @classmethod
    def reason_max_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > 500:
            raise ValueError("reason must be 500 characters or fewer")
        return v


class AppointmentResponse(BaseModel):
    appointment_id: int
    clinic_id: int
    dentist_user_id: int
    patient_user_id: int
    patient_name: Optional[str] = None
    # Teléfono para llamar o recordar por WhatsApp desde la ficha del turno. Las rutas de
    # lectura lo reemplazan por el vigente en `users` cuando lo tienen (#313); esto es sólo
    # el guardado al crear el turno, que queda como respaldo si el paciente no tiene uno hoy.
    patient_phone: Optional[str] = None
    start_time_utc: datetime
    end_time_utc: datetime
    patient_timezone: str
    status: AppointmentStatus
    google_event_id: Optional[str] = None
    gcal_sync_status: GcalSyncStatus = GcalSyncStatus.NOT_CONFIGURED
    reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AppointmentStatusUpdate(BaseModel):
    """
    Cambio de estado. El staff (ADMIN, RECEPTIONIST, odontólogo asignado) puede pasar a
    cualquier estado distinto del actual; el paciente sólo puede cancelar un turno
    programado o confirmado. Las reglas viven en el endpoint.
    """
    status: AppointmentStatus
    change_reason: Optional[str] = None


class AppointmentListResponse(BaseModel):
    appointments: list[AppointmentResponse]
    total: int


# ---------------------------------------------------------------------------
# Lista de espera (#298)
# ---------------------------------------------------------------------------

_NOTA_MAXIMA = 300


# «Desde cuándo le sirve»: nada anterior al 2000 ni más allá de dos años. Sin cota entraban el
# año 1 y el 9999, que no son una fecha que alguien quiera cargar sino un error o una prueba.
_DESDE_MINIMO = datetime(2000, 1, 1, tzinfo=timezone.utc)
_DESDE_MAXIMO = timedelta(days=2 * 366)


def _desde_razonable(v: datetime) -> datetime:
    v = _a_utc(v)
    if v < _DESDE_MINIMO or v > datetime.now(timezone.utc) + _DESDE_MAXIMO:
        raise ValueError("available_from_utc is out of range")
    return v


def _normalizar_nota(v: Optional[str]) -> Optional[str]:
    """Una nota en blanco no es una nota: se guarda NULL, no una cadena de espacios."""
    if v is None:
        return None
    v = v.strip()
    return v or None


class WaitlistEntryCreate(BaseModel):
    patient_user_id: int = Field(gt=0)
    patient_name: Optional[str] = Field(default=None, max_length=255)
    patient_phone: Optional[str] = Field(default=None, max_length=50)
    # None = cualquier odontólogo.
    dentist_user_id: Optional[int] = Field(default=None, gt=0)
    # Desde cuándo le sirve: el comienzo del día elegido, en el huso de quien anota.
    available_from_utc: datetime
    note: Optional[str] = Field(default=None, max_length=_NOTA_MAXIMA)

    @field_validator("available_from_utc")
    @classmethod
    def a_utc(cls, v: datetime) -> datetime:
        return _desde_razonable(v)

    @field_validator("note")
    @classmethod
    def nota(cls, v: Optional[str]) -> Optional[str]:
        return _normalizar_nota(v)


class WaitlistEntryUpdate(BaseModel):
    """
    Editar una entrada. Sólo se tocan los campos que vienen en el pedido: para pasar a
    «cualquier odontólogo» se manda `dentist_user_id: null` explícito, y para borrar la nota,
    `note: null`. Un campo que no viene queda como estaba.
    """
    dentist_user_id: Optional[int] = Field(default=None, gt=0)
    available_from_utc: Optional[datetime] = None
    note: Optional[str] = Field(default=None, max_length=_NOTA_MAXIMA)

    @field_validator("available_from_utc")
    @classmethod
    def a_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _desde_razonable(v) if v is not None else None

    @field_validator("note")
    @classmethod
    def nota(cls, v: Optional[str]) -> Optional[str]:
        return _normalizar_nota(v)

    @model_validator(mode="after")
    def desde_no_nulo(self):
        # `available_from_utc` es obligatorio en la tabla: mandarlo en null explícito no puede
        # significar «sin fecha».
        if "available_from_utc" in self.model_fields_set and self.available_from_utc is None:
            raise ValueError("available_from_utc cannot be null")
        return self


class AppointmentBrief(BaseModel):
    """El turno que ya tiene un paciente de la lista: el que se le puede adelantar."""
    appointment_id: int
    dentist_user_id: int
    start_time_utc: datetime
    end_time_utc: datetime

    model_config = ConfigDict(from_attributes=True)


class WaitlistEntryResponse(BaseModel):
    entry_id: int
    patient_user_id: int
    patient_name: Optional[str] = None
    # Teléfono para Llamar o el recordatorio por WhatsApp. Las rutas de lectura lo reemplazan por
    # el vigente en `users` cuando lo tienen (#314, mismo mecanismo que AppointmentResponse en
    # #313); esto es sólo el guardado al anotar, que queda como respaldo si no hay uno hoy.
    patient_phone: Optional[str] = None
    dentist_user_id: Optional[int] = None
    available_from_utc: datetime
    note: Optional[str] = None
    status: WaitlistStatus
    appointment_id: Optional[int] = None
    created_at: datetime
    # En la lista: su próximo turno activo (con ese odontólogo, o con cualquiera si espera a
    # cualquiera). Como candidato de un hueco: ese mismo turno, que por regla es POSTERIOR al
    # hueco, o sea el que se adelanta. None si no tiene.
    next_appointment: Optional[AppointmentBrief] = None


class WaitlistListResponse(BaseModel):
    entries: list[WaitlistEntryResponse]
    total: int


class FreedSlotResponse(BaseModel):
    slot_id: int
    dentist_user_id: int
    start_time_utc: datetime
    end_time_utc: datetime
    reason: FreedSlotReason
    created_at: datetime
    # Si quien pide lo puede descartar. Un odontólogo no descarta un aviso que también tiene
    # candidatos de la recepción (se lo cerraría a ella): el front esconde el botón.
    can_dismiss: bool = True
    candidates: list[WaitlistEntryResponse]


class FreedSlotListResponse(BaseModel):
    slots: list[FreedSlotResponse]
    # Cuántos esperan (lo que ve quien pide), para el contador del botón: así la pantalla hace
    # UN pedido por recarga y no dos.
    waiting_count: int
