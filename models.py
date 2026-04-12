from sqlalchemy import Column, Integer, String, DateTime, Enum, Text, Boolean, func, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship
import enum
from datetime import datetime

class Base(DeclarativeBase):
    pass

class AppointmentStatus(enum.Enum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

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
    
    start_time_utc = Column(DateTime, nullable=False)
    end_time_utc = Column(DateTime, nullable=False)
    patient_timezone = Column(String(50), default="UTC")
    
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.REQUESTED)
    google_event_id = Column(String(255), nullable=True, index=True)
    
    reason = Column(Text, nullable=True)
    observations = Column(Text, nullable=True)
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class AppointmentAuditLog(Base):
    __tablename__ = "appointment_audit_logs"

    log_id = Column(Integer, primary_key=True, autoincrement=True)
    appointment_id = Column(Integer, ForeignKey("appointments.appointment_id"), nullable=False)
    changed_by_user_id = Column(Integer, nullable=False)
    previous_status = Column(String(50))
    new_status = Column(String(50))
    change_reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
