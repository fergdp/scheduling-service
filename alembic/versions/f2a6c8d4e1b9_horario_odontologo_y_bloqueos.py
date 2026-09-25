"""Horario de atención por odontólogo y bloqueos de agenda (#296)

Revision ID: f2a6c8d4e1b9
Revises: c6fe51fd89ff
Create Date: 2026-09-23

Cambios:
- dentist_schedule_slots: el horario semanal recurrente de un odontólogo, un rango por fila
  (varias filas por día para mañana/tarde con corte). Sin ninguna fila, el odontólogo queda
  disponible siempre — igual que antes de este feature.
- dentist_schedule_blocks: bloqueos puntuales con fecha (vacaciones, congreso, un trámite).

Sólo crea tablas: `appointments` no cambia de forma, así que el front y el backend viejos
siguen funcionando con esta base — la restricción nueva es opt-in por odontólogo.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = 'f2a6c8d4e1b9'
down_revision: Union[str, None] = 'c6fe51fd89ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dentist_schedule_slots',
        sa.Column('slot_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('clinic_id', sa.Integer(), nullable=False),
        sa.Column('dentist_user_id', sa.Integer(), nullable=False),
        sa.Column('weekday', sa.Integer(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('start_time < end_time', name='chk_schedule_slot_time_range'),
        sa.CheckConstraint('weekday >= 0 AND weekday <= 6', name='chk_schedule_slot_weekday'),
        sa.PrimaryKeyConstraint('slot_id'),
    )
    op.create_index(op.f('ix_dentist_schedule_slots_clinic_id'), 'dentist_schedule_slots',
                    ['clinic_id'], unique=False)
    # Sin índice aparte para dentist_user_id solo: el compuesto de abajo ya lo cubre como
    # left-prefix.
    op.create_index('ix_dentist_schedule_slots_lookup', 'dentist_schedule_slots',
                    ['dentist_user_id', 'clinic_id', 'weekday'], unique=False)

    op.create_table(
        'dentist_schedule_blocks',
        sa.Column('block_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('clinic_id', sa.Integer(), nullable=False),
        sa.Column('dentist_user_id', sa.Integer(), nullable=False),
        sa.Column('start_time_utc', sa.DateTime(), nullable=False),
        sa.Column('end_time_utc', sa.DateTime(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('start_time_utc < end_time_utc', name='chk_schedule_block_time_range'),
        sa.PrimaryKeyConstraint('block_id'),
    )
    op.create_index(op.f('ix_dentist_schedule_blocks_clinic_id'), 'dentist_schedule_blocks',
                    ['clinic_id'], unique=False)
    op.create_index('ix_dentist_schedule_blocks_lookup', 'dentist_schedule_blocks',
                    ['dentist_user_id', 'clinic_id', 'start_time_utc'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_dentist_schedule_blocks_lookup', table_name='dentist_schedule_blocks')
    op.drop_index(op.f('ix_dentist_schedule_blocks_clinic_id'), table_name='dentist_schedule_blocks')
    op.drop_table('dentist_schedule_blocks')

    op.drop_index('ix_dentist_schedule_slots_lookup', table_name='dentist_schedule_slots')
    op.drop_index(op.f('ix_dentist_schedule_slots_clinic_id'), table_name='dentist_schedule_slots')
    op.drop_table('dentist_schedule_slots')
