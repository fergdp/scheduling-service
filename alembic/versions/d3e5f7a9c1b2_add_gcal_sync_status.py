"""Add gcal_sync_status to appointments

Revision ID: d3e5f7a9c1b2
Revises: b1d3f7a9c2e5
Create Date: 2026-04-29

Permite al frontend mostrar si el turno está sincronizado con Google Calendar.
Turnos existentes sin google_event_id → NOT_CONFIGURED.
Turnos existentes con google_event_id → SYNCED (asumimos que estaban OK).
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = 'd3e5f7a9c1b2'
down_revision: Union[str, None] = 'b1d3f7a9c2e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'appointments',
        sa.Column(
            'gcal_sync_status',
            sa.Enum('NOT_CONFIGURED', 'SYNCED', 'FAILED'),
            nullable=False,
            server_default='NOT_CONFIGURED'
        )
    )
    # Turnos que ya tienen un evento en GCal → marcar como SYNCED
    op.execute(
        "UPDATE appointments SET gcal_sync_status = 'SYNCED' "
        "WHERE google_event_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column('appointments', 'gcal_sync_status')
