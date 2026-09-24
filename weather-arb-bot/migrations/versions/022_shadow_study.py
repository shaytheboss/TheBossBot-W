"""Tables for the shadow study (app/shadow/).

Two new tables. No existing table is altered, and neither new table has a
foreign key, so nothing here can constrain or block a production write.

  shadow_snapshots      one row per (bucket, hour) for open markets
  shadow_market_state   per market: tracking start, summary sent

taken_at gets a BRIN index: the table is append-only in time order, which is
the case BRIN is built for — a few kilobytes instead of a full btree.

Revision ID: 022_shadow_study
Revises: 021_admin_query_indexes
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "022_shadow_study"
down_revision = "021_admin_query_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shadow_snapshots",
        sa.Column("outcome_id", sa.Integer(), nullable=False),
        sa.Column("taken_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("hours_to_close", sa.REAL(), nullable=False),
        sa.Column("local_hour", sa.SmallInteger(), nullable=False),
        sa.Column("model_p", sa.REAL(), nullable=False),
        sa.Column("raw_p", sa.REAL(), nullable=False),
        sa.Column("normalized", sa.Boolean(), nullable=False),
        sa.Column("market_p", sa.REAL()),
        sa.Column("n_sources", sa.SmallInteger()),
        sa.Column("forecast_age_min", sa.Integer()),
        sa.Column("forecast_high_f", sa.REAL()),
        sa.Column("sigma", sa.REAL()),
        sa.PrimaryKeyConstraint("outcome_id", "taken_at"),
    )
    op.create_index("ix_shadow_market_time", "shadow_snapshots", ["market_id", "taken_at"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_shadow_taken_brin "
        "ON shadow_snapshots USING brin (taken_at)"
    )
    op.create_table(
        "shadow_market_state",
        sa.Column("market_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("first_seen_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("summary_sent_at", postgresql.TIMESTAMP(timezone=True)),
    )


def downgrade():
    op.drop_table("shadow_market_state")
    op.execute("DROP INDEX IF EXISTS ix_shadow_taken_brin")
    op.drop_index("ix_shadow_market_time", table_name="shadow_snapshots")
    op.drop_table("shadow_snapshots")
