from sqlalchemy import Column, Integer, String, DateTime, Enum, Text, Boolean, func, ForeignKey, CheckConstraint
from sqlalchemy.orm import DeclarativeBase, relationship
import enum
from datetime import datetime

class Base(DeclarativeBase):
    pass

class AppointmentStatus(enum.Enum):
    SCHEDULED  = "SCHEDULED"
    COMPLETED  = "COMPLETED"
    CANCELLED  = "CANCELLED"

class GcalSyncStatus(enum.Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"  # dentista sin Google Calendar conectado
    SYNCED         = "SYNCED"          # evento existe y está actualizado en GCal
    FAILED         = "FAILED"          # sincronización intentada pero falló

class DentistCalendarConfig(Base):
    __tablename__ = "dentist_calendar_configs"

    config_id = Column(Integer, primary_key=True, autoincrement=True)
    dentist_user_id = Column(Integer, nullable=False, index=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    google_email = Column(String(255), nullable=True)

    # Encrypted tokens
    google_access_token = Column(String(2048), nullable=True)
    google_refresh_token = Column(String(2048), nullable=True)
    token_expiry = Column(DateTime, nullable=True)

    sync_enabled = Column(Boolean, default=True)
    default_timezone = Column(String(50), default="UTC")
    default_appointment_duration_minutes = Column(Integer, default=30, nullable=False)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<DentistCalendarConfig(dentist={self.dentist_user_id}, clinic={self.clinic_id})>"

class Appointment(Base):
    __tablename__ = "appointments"

    appointment_id = Column(Integer, primary_key=True, autoincrement=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    dentist_user_id = Column(Integer, nullable=False, index=True)
    patient_user_id = Column(Integer, nullable=False, index=True)
    patient_name    = Column(String(255), nullable=True)
    patient_email   = Column(String(255), nullable=True)
    patient_phone   = Column(String(50),  nullable=True)
    patient_dni     = Column(String(50),  nullable=True)
    patient_address = Column(String(255), nullable=True)

    start_time_utc = Column(DateTime, nullable=False)
    end_time_utc = Column(DateTime, nullable=False)
    patient_timezone = Column(String(50), default="UTC")

    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.SCHEDULED, nullable=False)
    google_event_id = Column(String(255), nullable=True, unique=True)
    gcal_sync_status = Column(
        Enum(GcalSyncStatus),
        default=GcalSyncStatus.NOT_CONFIGURED,
        nullable=False
    )

    reason = Column(Text, nullable=True)
    observations = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("start_time_utc < end_time_utc", name="chk_appointments_time_range"),
    )

class AppointmentAuditLog(Base):
    __tablename__ = "appointment_audit_logs"

    log_id = Column(Integer, primary_key=True, autoincrement=True)
    appointment_id = Column(Integer, ForeignKey("appointments.appointment_id"), nullable=False)
    changed_by_user_id = Column(Integer, nullable=False)
    previous_status = Column(String(50))
    new_status = Column(String(50))
    change_reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
