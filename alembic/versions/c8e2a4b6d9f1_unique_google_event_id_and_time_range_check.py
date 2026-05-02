"""UNIQUE en google_event_id y CHECK (start_time_utc < end_time_utc)

Revision ID: c8e2a4b6d9f1
Revises: f5a8b1c3d7e9
Create Date: 2026-05-02

Changes:
- DROP INDEX ix_appointments_google_event_id (sustituido por UNIQUE).
- ADD UNIQUE uq_appointments_google_event_id en appointments(google_event_id).
  → Evita que dos turnos compartan el mismo evento de Google Calendar.
  → La columna es nullable: en MySQL/InnoDB varios NULL conviven con UNIQUE.
  → ANTES DE APLICAR EN PROD: verificar que no haya duplicados.
        SELECT google_event_id, COUNT(*) FROM appointments
        WHERE google_event_id IS NOT NULL
        GROUP BY google_event_id HAVING COUNT(*) > 1;
- ADD CHECK chk_appointments_time_range (start_time_utc < end_time_utc).
  → Requiere MySQL 8.0.16+. Guardrail a nivel DB; la validación de app sigue.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c8e2a4b6d9f1'
down_revision: Union[str, None] = 'f5a8b1c3d7e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Reemplazar el índice no-único por UNIQUE constraint.
    op.drop_index('ix_appointments_google_event_id', table_name='appointments')
    op.create_unique_constraint(
        'uq_appointments_google_event_id',
        'appointments',
        ['google_event_id']
    )

    # 2. CHECK constraint: rango temporal coherente.
    op.create_check_constraint(
        'chk_appointments_time_range',
        'appointments',
        'start_time_utc < end_time_utc'
    )


def downgrade() -> None:
    op.drop_constraint('chk_appointments_time_range', 'appointments', type_='check')
    op.drop_constraint('uq_appointments_google_event_id', 'appointments', type_='unique')
    op.create_index(
        'ix_appointments_google_event_id',
        'appointments',
        ['google_event_id'],
        unique=False
    )
