"""Add unique constraint on dentist_calendar_configs and composite index on appointments

Revision ID: a3f92c1d8e47
Revises: 01496af57eb6
Create Date: 2026-04-14

Changes:
- UNIQUE(dentist_user_id, clinic_id) on dentist_calendar_configs
  → Prevents duplicate Google Calendar configs for same dentist+clinic pair.
  → The code relies on .first() assuming uniqueness; this enforces it at DB level.

- Composite index (clinic_id, dentist_user_id, start_time_utc) on appointments
  → Availability queries filter by these three columns; without this index
    they do a full table scan as appointment volume grows.

- Index on appointment_audit_logs(appointment_id)
  → Audit log queries join on this column; missing index causes slow lookups.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a3f92c1d8e47'
down_revision: Union[str, None] = '01496af57eb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. UNIQUE constraint: one Google Calendar config per dentist per clinic.
    #    Before adding: remove any duplicate rows that may already exist in prod.
    #    Safe to run — if duplicates exist, this will raise; clean them first.
    op.create_unique_constraint(
        'uq_dentist_calendar_configs_dentist_clinic',
        'dentist_calendar_configs',
        ['dentist_user_id', 'clinic_id']
    )

    # 2. Composite index for availability range queries on appointments.
    #    Covers: WHERE clinic_id = ? AND dentist_user_id = ? AND start_time_utc >= ?
    op.create_index(
        'ix_appointments_clinic_dentist_start',
        'appointments',
        ['clinic_id', 'dentist_user_id', 'start_time_utc'],
        unique=False
    )

    # 3. Index on audit log's appointment_id FK (missing from initial migration).
    op.create_index(
        'ix_appointment_audit_logs_appointment_id',
        'appointment_audit_logs',
        ['appointment_id'],
        unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_appointment_audit_logs_appointment_id', table_name='appointment_audit_logs')
    op.drop_index('ix_appointments_clinic_dentist_start', table_name='appointments')
    op.drop_constraint(
        'uq_dentist_calendar_configs_dentist_clinic',
        'dentist_calendar_configs',
        type_='unique'
    )
