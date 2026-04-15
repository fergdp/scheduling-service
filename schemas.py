from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from datetime import datetime, timezone
from typing import Optional
from models import AppointmentStatus

# Base schemas for OAuth
class OAuthUrlResponse(BaseModel):
    auth_url: str

class GoogleConfigBase(BaseModel):
    sync_enabled: bool = True
    default_timezone: str = "UTC"

class GoogleConfigResponse(GoogleConfigBase):
    config_id: int
    dentist_user_id: int
    clinic_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

# Appointment schemas
class AppointmentBase(BaseModel):
    dentist_user_id: int
    start_time_utc: datetime
    end_time_utc: datetime
    patient_timezone: str = "UTC"
    reason: Optional[str] = None

class AppointmentCreate(AppointmentBase):

    @field_validator("start_time_utc")
    @classmethod
    def start_must_be_future(cls, v: datetime) -> datetime:
        now = datetime.now(timezone.utc)
        # Normalizar a UTC para comparar (si viene sin tzinfo, asumir UTC)
        v_aware = v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
        if v_aware <= now:
            raise ValueError("start_time_utc must be in the future")
        return v_aware  # normalizar siempre a UTC-aware antes de persistir

    @field_validator("reason")
    @classmethod
    def reason_max_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > 500:
            raise ValueError("reason must be 500 characters or fewer")
        return v

    @model_validator(mode="after")
    def end_must_be_after_start(self) -> "AppointmentCreate":
        if self.start_time_utc >= self.end_time_utc:
            raise ValueError("end_time_utc must be after start_time_utc")
        duration = self.end_time_utc - self.start_time_utc
        if duration.total_seconds() < 900:  # 15 minutos mínimo
            raise ValueError("Appointment duration must be at least 15 minutes")
        if duration.total_seconds() > 28800:  # 8 horas máximo
            raise ValueError("Appointment duration cannot exceed 8 hours")
        return self

class AppointmentResponse(AppointmentBase):
    appointment_id: int
    clinic_id: int
    patient_user_id: int
    status: AppointmentStatus
    google_event_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AppointmentStatusUpdate(BaseModel):
    status: AppointmentStatus
    change_reason: Optional[str] = None
