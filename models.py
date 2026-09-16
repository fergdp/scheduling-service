from sqlalchemy import Column, Integer, String, DateTime, Enum, Text, Boolean, func, ForeignKey, CheckConstraint, Index
from sqlalchemy.orm import DeclarativeBase, relationship
import enum
from datetime import datetime, timezone


def _ahora_utc() -> datetime:
    """
    UTC naive, como guardan las fechas todas las columnas de este servicio.

    Las tablas de la lista de espera NO usan sólo `server_default=now()`: el `NOW()` de MySQL
    devuelve la hora del huso del servidor, no UTC, y el front lee toda fecha sin huso como UTC
    (`toUtc`). Con un servidor en hora argentina, «anotado el 16/09» salía corrido tres horas y
    cerca de medianoche cambiaba de día.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

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


# ---------------------------------------------------------------------------
# Lista de espera (#298)
# ---------------------------------------------------------------------------

class WaitlistStatus(enum.Enum):
    WAITING = "WAITING"   # Espera que se libere un hueco
    BOOKED  = "BOOKED"    # Se le dio un turno desde la lista: `appointment_id` dice cuál
    REMOVED = "REMOVED"   # Lo sacaron de la lista. Lógico, para que quede el historial


class FreedSlotReason(enum.Enum):
    CANCELLED   = "CANCELLED"    # El turno se canceló
    RESCHEDULED = "RESCHEDULED"  # El turno se pasó a otro horario o a otro odontólogo


class FreedSlotCloseReason(enum.Enum):
    DISMISSED  = "DISMISSED"   # Alguien tocó Descartar
    SUPERSEDED = "SUPERSEDED"  # El mismo horario se volvió a liberar: vale el aviso nuevo
    FILLED     = "FILLED"      # El horario se volvió a ocupar (turno nuevo, movido o reactivado)
    DELETED    = "DELETED"     # Se borró el turno que lo liberó: estaba mal cargado, no liberó nada


class WaitlistEntry(Base):
    """
    Un paciente que quiere un turno antes del que consiguió, o que no consiguió ninguno.

    `dentist_user_id` NULL es «cualquier odontólogo». `available_from_utc` es el comienzo del día
    desde el que le sirve, calculado en el huso de quien lo anotó: así este servicio no necesita
    saber en qué huso está la clínica.
    """
    __tablename__ = "waitlist_entries"

    entry_id = Column(Integer, primary_key=True, autoincrement=True)
    clinic_id = Column(Integer, nullable=False)
    patient_user_id = Column(Integer, nullable=False, index=True)
    patient_name = Column(String(255), nullable=True)
    patient_phone = Column(String(50), nullable=True)
    dentist_user_id = Column(Integer, nullable=True, index=True)
    available_from_utc = Column(DateTime, nullable=False)
    note = Column(Text, nullable=True)
    status = Column(Enum(WaitlistStatus), default=WaitlistStatus.WAITING, nullable=False)
    appointment_id = Column(Integer, ForeignKey("appointments.appointment_id"), nullable=True)

    created_by_user_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_ahora_utc, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=_ahora_utc, onupdate=_ahora_utc,
                        server_default=func.now(), nullable=False)
    closed_at = Column(DateTime, nullable=True)
    closed_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        # La consulta de siempre: los que esperan en esta clínica.
        Index("ix_waitlist_entries_clinic_status", "clinic_id", "status"),
    )


class FreedSlot(Base):
    """
    Un horario que quedó libre porque un turno futuro se canceló o se reprogramó: el aviso de la
    lista de espera. Se muestra mientras siga libre, no haya empezado, no esté cerrado y alguien
    de la lista lo quiera; esas dos últimas cosas se calculan al leer, no se guardan.
    """
    __tablename__ = "freed_slots"

    slot_id = Column(Integer, primary_key=True, autoincrement=True)
    clinic_id = Column(Integer, nullable=False)
    dentist_user_id = Column(Integer, nullable=False, index=True)
    start_time_utc = Column(DateTime, nullable=False)
    end_time_utc = Column(DateTime, nullable=False)
    source_appointment_id = Column(Integer, ForeignKey("appointments.appointment_id"), nullable=False)
    reason = Column(Enum(FreedSlotReason), nullable=False)

    created_by_user_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_ahora_utc, server_default=func.now(), nullable=False)
    closed_at = Column(DateTime, nullable=True)
    closed_by_user_id = Column(Integer, nullable=True)
    close_reason = Column(Enum(FreedSlotCloseReason), nullable=True)

    __table_args__ = (
        # La consulta del cartel: los avisos abiertos de la clínica que todavía no empezaron.
        Index("ix_freed_slots_open", "clinic_id", "closed_at", "start_time_utc"),
    )
