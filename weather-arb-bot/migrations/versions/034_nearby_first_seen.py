"""nearby_signals.first_seen_at: when the signal first fired, tradable or not.

One nullable column — catalog-only in Postgres. Earlier rows stay NULL.

Revision ID: 034_nearby_first_seen
Revises: 033_nearby_tradable
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "034_nearby_first_seen"
down_revision = "033_nearby_tradable"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("nearby_signals", sa.Column("first_seen_at", postgresql.TIMESTAMP(timezone=True), nullable=True))


def downgrade():
    op.drop_column("nearby_signals", "first_seen_at")
