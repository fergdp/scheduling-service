from sqlalchemy import Column, Integer, String, DateTime, Enum, Text, Boolean, func, ForeignKey, CheckConstraint
from sqlalchemy.orm import DeclarativeBase, relationship
import enum
from datetime import datetime

class Base(DeclarativeBase):
    pass

class AppointmentStatus(enum.Enum):
    # Los tres "activos" ocupan el hueco del odontólogo; los otros tres lo liberan.
    SCHEDULED  = "SCHEDULED"   # Programado: se dio el turno
    CONFIRMED  = "CONFIRMED"   # Confirmado: el paciente confirmó por teléfono/WhatsApp
    ARRIVED    = "ARRIVED"     # En espera: el paciente llegó y está en la sala
    COMPLETED  = "COMPLETED"   # Atendido
    CANCELLED  = "CANCELLED"   # Cancelado: avisó que no viene
    NO_SHOW    = "NO_SHOW"     # Ausente: no vino y no avisó


# Estados que reservan el hueco: cuentan para el solapamiento, la disponibilidad y el
# widget de próximos turnos. Entrar a uno de estos desde uno inactivo vuelve a chequear
# solapamiento, porque el hueco pudo ocuparse mientras el turno estaba cancelado.
ACTIVE_STATUSES = frozenset({
    AppointmentStatus.SCHEDULED,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.ARRIVED,
})

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

    # Borrado lógico: "lo cargué mal", distinto de CANCELLED ("el paciente avisó que no viene",
    # que queda en el historial). Un turno con deleted_at no se lee desde ningún endpoint;
    # la fila se conserva porque appointment_audit_logs tiene FK a este id.
    deleted_at = Column(DateTime, nullable=True)
    deleted_by_user_id = Column(Integer, nullable=True)

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
