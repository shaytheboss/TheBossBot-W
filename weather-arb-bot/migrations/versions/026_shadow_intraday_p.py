"""Shadow study: record the intraday model's probability next to the daily one.

One nullable REAL column on shadow_snapshots. Adding a nullable column with no
default is a catalog-only change in Postgres — no rewrite of existing rows.

Revision ID: 026_shadow_intraday_p
Revises: 025_model_update_events
"""
from alembic import op
import sqlalchemy as sa

revision = "026_shadow_intraday_p"
down_revision = "025_model_update_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("shadow_snapshots", sa.Column("intraday_p", sa.REAL(), nullable=True))


def downgrade():
    op.drop_column("shadow_snapshots", "intraday_p")
