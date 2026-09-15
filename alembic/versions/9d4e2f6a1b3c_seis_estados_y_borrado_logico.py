"""Seis estados de turno + borrado lógico (agenda de recepción)

Revision ID: 9d4e2f6a1b3c
Revises: c8e2a4b6d9f1
Create Date: 2026-09-15

Cambios:
- appointments.status: el ENUM pasa de 3 a 6 valores. Nuevos: CONFIRMED (confirmado por el
  paciente), ARRIVED (en sala de espera), NO_SHOW (ausente). Ningún dato cambia: los tres
  valores anteriores siguen siendo válidos, así que el front viejo sigue funcionando.
- appointments.deleted_at / deleted_by_user_id: borrado lógico ("lo cargué mal"), distinto
  de CANCELLED, que queda en el historial. La fila se conserva porque
  appointment_audit_logs tiene FK al turno.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = '9d4e2f6a1b3c'
down_revision: Union[str, None] = 'c8e2a4b6d9f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Ampliar el ENUM (MySQL). Mismo patrón que b1d3f7a9c2e5.
    op.execute(
        "ALTER TABLE appointments "
        "MODIFY COLUMN status "
        "ENUM('SCHEDULED','CONFIRMED','ARRIVED','COMPLETED','CANCELLED','NO_SHOW') "
        "NOT NULL DEFAULT 'SCHEDULED'"
    )

    # 2. Borrado lógico
    op.add_column('appointments', sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('appointments', sa.Column('deleted_by_user_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    # Un turno borrado no puede volver como SCHEDULED activo al perder deleted_at: quedaría
    # ocupando un hueco que probablemente ya se usó. Pasa a CANCELLED antes de perder la marca.
    op.execute("UPDATE appointments SET status = 'CANCELLED' WHERE deleted_at IS NOT NULL")
    op.drop_column('appointments', 'deleted_by_user_id')
    op.drop_column('appointments', 'deleted_at')

    # Mapear los estados nuevos a los viejos ANTES de achicar el ENUM: los activos vuelven a
    # SCHEDULED (siguen ocupando el hueco) y el ausente pasa a CANCELLED (lo libera).
    op.execute(
        "UPDATE appointments SET status = 'SCHEDULED' "
        "WHERE status IN ('CONFIRMED', 'ARRIVED')"
    )
    op.execute(
        "UPDATE appointments SET status = 'CANCELLED' WHERE status = 'NO_SHOW'"
    )
    op.execute(
        "ALTER TABLE appointments "
        "MODIFY COLUMN status ENUM('SCHEDULED','COMPLETED','CANCELLED') "
        "NOT NULL DEFAULT 'SCHEDULED'"
    )
