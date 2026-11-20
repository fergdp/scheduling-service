"""Tabla contact_marks: «A quién escribirle hoy» (#556)

Revision ID: b4d8e2f6a9c1
Revises: a7c4e9d2b6f3
Create Date: 2026-11-20

Crea una tabla nueva y nada más: no toca ni un dato de las que ya están. Guarda lo que hizo el
consultorio con cada aviso del panel (le escribió, lo pospuso, no sugerir más) y cada pedido de
reseña. De sólo agregar: deshacer es otra fila con `anula_a_id`.

Sus dos columnas con un usuario están en `DatosQueSeConservan.NO_FRENAN` de dental-clinic (#375):
una traza que no impide borrar a nadie. dental-clinic no consulta esta tabla, así que no hay orden
de subida con su JAR. El que va antes es el GRANT de `paso_556_grant.sh` (lo que el motor lee de
otras bases), y sin él `GET /contactos/hoy` sale vacío con los motivos en `motivos_con_error`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'b4d8e2f6a9c1'
down_revision: Union[str, None] = 'a7c4e9d2b6f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLA = 'contact_marks'
MOTIVOS = ("CONTROL", "PRESUPUESTO", "RESENA")
ACCIONES = ("ESCRITO", "POSPUESTO", "NO_SUGERIR", "NO_SUGERIR_NADA", "DESHECHO")


def _en_lista(columna, valores):
    return f"{columna} IN ({', '.join(repr(v) for v in valores)})"


def upgrade() -> None:
    op.create_table(
        TABLA,
        sa.Column('mark_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('clinic_id', sa.Integer(), nullable=False),
        sa.Column('patient_user_id', sa.Integer(), nullable=False),
        sa.Column('motivo', sa.String(16), nullable=False),
        sa.Column('quote_id', sa.Integer(), nullable=True),
        sa.Column('accion', sa.String(16), nullable=False),
        sa.Column('fecha', sa.Date(), nullable=False),
        sa.Column('hasta', sa.Date(), nullable=True),
        sa.Column('anula_a_id', sa.Integer(), sa.ForeignKey(f'{TABLA}.mark_id'), nullable=True),
        sa.Column('marked_by_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('anula_a_id', name='uq_contact_marks_anula_a_id'),
        sa.CheckConstraint(_en_lista('motivo', MOTIVOS), name='chk_contact_marks_motivo'),
        sa.CheckConstraint(_en_lista('accion', ACCIONES), name='chk_contact_marks_accion'),
    )
    op.create_index('ix_contact_marks_clinic_patient', TABLA, ['clinic_id', 'patient_user_id'])


def downgrade() -> None:
    op.drop_index('ix_contact_marks_clinic_patient', table_name=TABLA)
    op.drop_table(TABLA)
