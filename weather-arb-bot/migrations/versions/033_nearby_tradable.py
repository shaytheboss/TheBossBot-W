"""nearby_signals.tradable: could the paper buy have been made at that ask?

One nullable boolean column — catalog-only in Postgres, no rewrite. Rows
recorded before it existed stay NULL and count as not tradable.

Revision ID: 033_nearby_tradable
Revises: 032_nearby_signals
"""
from alembic import op
import sqlalchemy as sa

revision = "033_nearby_tradable"
down_revision = "032_nearby_signals"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("nearby_signals", sa.Column("tradable", sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column("nearby_signals", "tradable")
