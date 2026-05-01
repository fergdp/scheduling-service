"""Refactor appointment status + add default duration per dentist

Revision ID: b1d3f7a9c2e5
Revises: a3f92c1d8e47
Create Date: 2026-04-29

Changes:
- appointments.status: REQUESTED/APPROVED/REJECTED eliminados → SCHEDULED nuevo.
  Datos existentes: REQUESTED+APPROVED → SCHEDULED, REJECTED → CANCELLED.
- dentist_calendar_configs: nuevo campo default_appointment_duration_minutes (default 30).
- appointments: nuevo campo patient_name (VARCHAR 255, nullable).
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = 'b1d3f7a9c2e5'
down_revision: Union[str, None] = 'a3f92c1d8e47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Nuevo campo: duración default por odontólogo
    op.add_column(
        'dentist_calendar_configs',
        sa.Column('default_appointment_duration_minutes', sa.Integer(),
                  nullable=False, server_default='30')
    )

    # 2. Nuevo campo: nombre del paciente desnormalizado para display
    op.add_column(
        'appointments',
        sa.Column('patient_name', sa.String(255), nullable=True)
    )

    # 3. Migrar datos antes de cambiar el ENUM
    #    REQUESTED/APPROVED → SCHEDULED (turno confirmado)
    #    REJECTED → CANCELLED
    op.execute(
        "UPDATE appointments SET status = 'SCHEDULED' "
        "WHERE status IN ('REQUESTED', 'APPROVED')"
    )
    op.execute(
        "UPDATE appointments SET status = 'CANCELLED' "
        "WHERE status = 'REJECTED'"
    )

    # 4. Cambiar el ENUM en MySQL
    op.execute(
        "ALTER TABLE appointments "
        "MODIFY COLUMN status ENUM('SCHEDULED','COMPLETED','CANCELLED') "
        "NOT NULL DEFAULT 'SCHEDULED'"
    )


def downgrade() -> None:
    # Revertir ENUM (datos SCHEDULED → REQUESTED; no se puede recuperar REJECTED)
    op.execute(
        "ALTER TABLE appointments "
        "MODIFY COLUMN status "
        "ENUM('REQUESTED','APPROVED','REJECTED','CANCELLED','COMPLETED') "
        "NOT NULL DEFAULT 'REQUESTED'"
    )
    op.execute(
        "UPDATE appointments SET status = 'REQUESTED' WHERE status = 'SCHEDULED'"
    )
    op.drop_column('appointments', 'patient_name')
    op.drop_column('dentist_calendar_configs', 'default_appointment_duration_minutes')
