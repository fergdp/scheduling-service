"""Lista de espera y huecos liberados (#298)

Revision ID: 7b3e9c1f5a2d
Revises: 9d4e2f6a1b3c
Create Date: 2026-09-16

Cambios:
- waitlist_entries: los pacientes que esperan un turno antes. `dentist_user_id` NULL es
  «cualquier odontólogo». Sacar a alguien de la lista es lógico (status REMOVED).
- freed_slots: los horarios que quedaron libres al cancelar o reprogramar un turno futuro, que
  es lo que muestra el cartel de la agenda.

Sólo crea tablas: `appointments` no cambia, así que el front y el backend viejos siguen
funcionando con esta base.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import context, op

revision: str = '7b3e9c1f5a2d'
down_revision: Union[str, None] = '9d4e2f6a1b3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tipo_del_id_de_turno():
    """
    El tipo REAL de `appointments.appointment_id` en esta base.

    ⚠️ No es el mismo en todos lados: la migración inicial lo crea `INTEGER`, pero la base local
    de desarrollo lo tiene `BIGINT` (se creó por otro camino). MySQL exige que la columna de una
    clave foránea tenga exactamente el tipo de la referenciada, y con `sa.Integer()` fijo el
    `CREATE TABLE` falla con el error 3780. Se lee de la base en vez de suponerlo.
    """
    # `alembic upgrade --sql` (modo offline) no tiene base a la que preguntar: ahí va el tipo de
    # la migración inicial, que es el de las bases creadas con Alembic.
    if context.is_offline_mode():
        return sa.Integer()
    columnas = sa.inspect(op.get_bind()).get_columns('appointments')
    return next(c['type'] for c in columnas if c['name'] == 'appointment_id')


def upgrade() -> None:
    tipo_id_turno = _tipo_del_id_de_turno()

    op.create_table(
        'waitlist_entries',
        sa.Column('entry_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('clinic_id', sa.Integer(), nullable=False),
        sa.Column('patient_user_id', sa.Integer(), nullable=False),
        sa.Column('patient_name', sa.String(length=255), nullable=True),
        sa.Column('patient_phone', sa.String(length=50), nullable=True),
        sa.Column('dentist_user_id', sa.Integer(), nullable=True),
        sa.Column('available_from_utc', sa.DateTime(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column(
            'status',
            sa.Enum('WAITING', 'BOOKED', 'REMOVED', name='waitliststatus'),
            nullable=False,
            server_default='WAITING',
        ),
        sa.Column('appointment_id', tipo_id_turno, nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('closed_by_user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['appointment_id'], ['appointments.appointment_id']),
        sa.PrimaryKeyConstraint('entry_id'),
    )
    op.create_index('ix_waitlist_entries_clinic_status', 'waitlist_entries',
                    ['clinic_id', 'status'], unique=False)
    op.create_index(op.f('ix_waitlist_entries_patient_user_id'), 'waitlist_entries',
                    ['patient_user_id'], unique=False)
    op.create_index(op.f('ix_waitlist_entries_dentist_user_id'), 'waitlist_entries',
                    ['dentist_user_id'], unique=False)

    op.create_table(
        'freed_slots',
        sa.Column('slot_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('clinic_id', sa.Integer(), nullable=False),
        sa.Column('dentist_user_id', sa.Integer(), nullable=False),
        sa.Column('start_time_utc', sa.DateTime(), nullable=False),
        sa.Column('end_time_utc', sa.DateTime(), nullable=False),
        sa.Column('source_appointment_id', tipo_id_turno, nullable=False),
        sa.Column(
            'reason',
            sa.Enum('CANCELLED', 'RESCHEDULED', name='freedslotreason'),
            nullable=False,
        ),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('closed_by_user_id', sa.Integer(), nullable=True),
        sa.Column(
            'close_reason',
            sa.Enum('DISMISSED', 'SUPERSEDED', 'FILLED', 'DELETED', name='freedslotclosereason'),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(['source_appointment_id'], ['appointments.appointment_id']),
        sa.PrimaryKeyConstraint('slot_id'),
    )
    op.create_index('ix_freed_slots_open', 'freed_slots',
                    ['clinic_id', 'closed_at', 'start_time_utc'], unique=False)
    op.create_index(op.f('ix_freed_slots_dentist_user_id'), 'freed_slots',
                    ['dentist_user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_freed_slots_dentist_user_id'), table_name='freed_slots')
    op.drop_index('ix_freed_slots_open', table_name='freed_slots')
    op.drop_table('freed_slots')

    op.drop_index(op.f('ix_waitlist_entries_dentist_user_id'), table_name='waitlist_entries')
    op.drop_index(op.f('ix_waitlist_entries_patient_user_id'), table_name='waitlist_entries')
    op.drop_index('ix_waitlist_entries_clinic_status', table_name='waitlist_entries')
    op.drop_table('waitlist_entries')
