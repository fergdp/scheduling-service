from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional, List
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
    pass

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
