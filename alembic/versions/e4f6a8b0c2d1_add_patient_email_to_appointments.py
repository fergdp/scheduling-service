"""add patient_email to appointments

Revision ID: e4f6a8b0c2d1
Revises: d3e5f7a9c1b2
Create Date: 2026-04-29

"""
from alembic import op
import sqlalchemy as sa

revision = 'e4f6a8b0c2d1'
down_revision = 'd3e5f7a9c1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('appointments', sa.Column('patient_email', sa.String(255), nullable=True))


def downgrade():
    op.drop_column('appointments', 'patient_email')
