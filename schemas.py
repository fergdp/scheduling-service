from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import datetime, timezone
from typing import Optional
from models import AppointmentStatus, GcalSyncStatus


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
    dentist_user_id: int
    patient_user_id: int
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
        now = datetime.now(timezone.utc)
        v_aware = v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
        if v_aware <= now:
            raise ValueError("start_time_utc must be in the future")
        return v_aware

    @field_validator("reason")
    @classmethod
    def reason_max_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > 500:
            raise ValueError("reason must be 500 characters or fewer")
        return v

    @field_validator("end_time_utc")
    @classmethod
    def normalize_end_time(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


class AppointmentUpdate(BaseModel):
    """Editar fecha/hora y/o motivo de un turno existente."""
    start_time_utc: Optional[datetime] = None
    end_time_utc: Optional[datetime] = None
    reason: Optional[str] = None

    @field_validator("start_time_utc")
    @classmethod
    def start_must_be_future(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        now = datetime.now(timezone.utc)
        v_aware = v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
        if v_aware <= now:
            raise ValueError("start_time_utc must be in the future")
        return v_aware

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
    """Solo se permiten transiciones SCHEDULED→COMPLETED y SCHEDULED→CANCELLED."""
    status: AppointmentStatus
    change_reason: Optional[str] = None


class AppointmentListResponse(BaseModel):
    appointments: list[AppointmentResponse]
    total: int
