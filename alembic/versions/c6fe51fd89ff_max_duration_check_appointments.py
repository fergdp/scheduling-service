"""CHECK de duración máxima en appointments (issue #319)

Revision ID: c6fe51fd89ff
Revises: 7b3e9c1f5a2d
Create Date: 2026-09-21 15:59:11.680379

Changes:
- ADD CHECK chk_appointments_max_duration
  (TIMESTAMPDIFF(SECOND, start_time_utc, end_time_utc) <= 28800).
  → Requiere MySQL 8.0.16+, mismo requisito que chk_appointments_time_range (#f5a8b1c3d7e9→c8e2a4b6d9f1).
  → El tope de 8h (28800s) ya lo aplica `_validate_times` en Python al crear/editar un turno;
    esto es el guardrail a nivel DB que faltaba. Sin él, el #309 (la cota de abajo del chequeo
    de solapamiento, que asume que ningún turno activo dura más de 8h) descansaba sólo en la
    validación de la aplicación — y hubo una ventana real (2026-04-11 a 14, antes de que
    `_validate_times` existiera) sin ninguna validación de duración.
  → ANTES DE APLICAR EN PROD: verificar que no haya filas que ya violen el tope, o el ALTER
    falla al crear el CHECK (MySQL valida las filas existentes).
        SELECT appointment_id, start_time_utc, end_time_utc, status FROM appointments
        WHERE TIMESTAMPDIFF(SECOND, start_time_utc, end_time_utc) > 28800
          AND deleted_at IS NULL;
    Si aparece alguna fila, corregirla (o marcarla deleted_at) antes de correr esta migración —
    no se intenta arreglar solo, porque cambiar start_time_utc/end_time_utc de un turno real
    sin que nadie lo pida es una decisión que no le corresponde a una migración.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c6fe51fd89ff'
down_revision: Union[str, None] = '7b3e9c1f5a2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        'chk_appointments_max_duration',
        'appointments',
        'TIMESTAMPDIFF(SECOND, start_time_utc, end_time_utc) <= 28800',
    )


def downgrade() -> None:
    op.drop_constraint('chk_appointments_max_duration', 'appointments', type_='check')
