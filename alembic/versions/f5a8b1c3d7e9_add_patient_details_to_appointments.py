"""add patient details to appointments

Revision ID: f5a8b1c3d7e9
Revises: e4f6a8b0c2d1
Create Date: 2026-04-30

"""
from alembic import op
import sqlalchemy as sa

revision = 'f5a8b1c3d7e9'
down_revision = 'e4f6a8b0c2d1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('appointments', sa.Column('patient_phone',   sa.String(50),  nullable=True))
    op.add_column('appointments', sa.Column('patient_dni',     sa.String(50),  nullable=True))
    op.add_column('appointments', sa.Column('patient_address', sa.String(255), nullable=True))


def downgrade():
    op.drop_column('appointments', 'patient_address')
    op.drop_column('appointments', 'patient_dni')
    op.drop_column('appointments', 'patient_phone')
