"""Shadow study: live order-book prices and a freshness stamp.

Adds four nullable columns to shadow_snapshots. The table is research-only and
nothing in production reads it, so a plain ADD COLUMN is safe on a live
database — nullable columns with no default are a catalog-only change in
Postgres, with no table rewrite.

  bid, ask            the executable prices at snapshot time
  price_live          true when market_p came from the live order book,
                      false when it fell back to the stored midpoint
  price_job_age_min   minutes since the price job last ran

Revision ID: 023_shadow_live_prices
Revises: 022_shadow_study
"""
from alembic import op
import sqlalchemy as sa

revision = "023_shadow_live_prices"
down_revision = "022_shadow_study"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("shadow_snapshots", sa.Column("bid", sa.REAL()))
    op.add_column("shadow_snapshots", sa.Column("ask", sa.REAL()))
    op.add_column("shadow_snapshots", sa.Column("price_live", sa.Boolean()))
    op.add_column("shadow_snapshots", sa.Column("price_job_age_min", sa.Integer()))


def downgrade():
    for col in ("price_job_age_min", "price_live", "ask", "bid"):
        op.drop_column("shadow_snapshots", col)
