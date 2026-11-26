"""Tabla contact_settings: la configuración de «A quién escribirle hoy» (#558)

Revision ID: c7e3a1f5b8d2
Revises: b4d8e2f6a9c1
Create Date: 2026-11-26

Crea una tabla nueva y nada más. Una fila por clínica con el link de reseña de Google, los textos de
los mensajes, cada cuánto se sugiere el control y el tope del día. Sin fila (o con un campo vacío)
vale lo de fábrica, así que la tabla nace vacía y nada cambia hasta que alguien guarda.

Sin columnas de usuario: no entra en la regla de borrado de dental-clinic (#375).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'c7e3a1f5b8d2'
down_revision: Union[str, None] = 'b4d8e2f6a9c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'contact_settings',
        sa.Column('clinic_id', sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column('link_resena', sa.String(500), nullable=True),
        sa.Column('texto_control', sa.Text(), nullable=True),
        sa.Column('texto_chicos', sa.Text(), nullable=True),
        sa.Column('texto_presupuesto', sa.Text(), nullable=True),
        sa.Column('texto_resena', sa.Text(), nullable=True),
        sa.Column('dias_control', sa.Integer(), nullable=True),
        sa.Column('tope_diario', sa.Integer(), nullable=True),
        sa.Column('motivos_activos', sa.String(64), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        # Los textos van por WhatsApp y pueden traer emojis: utf8mb4 explícito, sin depender del
        # charset que tenga la base (SQLite ignora estos dos).
        mysql_charset='utf8mb4',
        mysql_collate='utf8mb4_unicode_ci',
    )


def downgrade() -> None:
    op.drop_table('contact_settings')
