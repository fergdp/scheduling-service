import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import datetime, timezone
from typing import Optional
from models import AppointmentStatus, GcalSyncStatus

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
    return v.astimezone(timezone.utc) if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


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
    patient_name: Optional[str] = None
    patient_email: Optional[str] = None
    patient_phone: Optional[str] = None
    patient_dni: Optional[str] = None
    patient_address: Optional[str] = None
    start_time_utc: datetime
    end_time_utc: Optional[datetime] = None  # si no se envía, usa la duración default del odontólogo
    patient_timezone: str = "UTC"
    reason: Optional[str] = None

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
    # Teléfono para que recepción pueda llamar desde la ficha del turno.
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
