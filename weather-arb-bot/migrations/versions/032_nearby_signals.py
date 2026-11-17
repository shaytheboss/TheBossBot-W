"""Paper trades on the nearby-station signal (app/nearby/paper.py).

One new, small table — at most one row per market; no existing table is touched.

Revision ID: 032_nearby_signals
Revises: 031_nws_observations
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "032_nearby_signals"
down_revision = "031_nws_observations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nearby_signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("outcome_id", sa.Integer(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("signaled_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("local_hour", sa.REAL(), nullable=False),
        sa.Column("icao", sa.String(4), nullable=False),
        sa.Column("max_f", sa.REAL(), nullable=False),
        sa.Column("current_f", sa.REAL(), nullable=False),
        sa.Column("n_nearby", sa.SmallInteger(), nullable=False),
        sa.Column("n_falling", sa.SmallInteger(), nullable=False),
        sa.Column("detail", sa.JSON()),
        sa.Column("bid", sa.REAL()),
        sa.Column("ask", sa.REAL()),
        sa.Column("ask_size", sa.REAL()),
        sa.Column("mid_10", sa.REAL()),
        sa.Column("mid_30", sa.REAL()),
        sa.Column("alerted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("market_id", name="uq_nearby_signal_market"),
    )
    op.create_index("idx_nearby_signal_at", "nearby_signals", ["signaled_at"])


def downgrade():
    op.drop_index("idx_nearby_signal_at", table_name="nearby_signals")
    op.drop_table("nearby_signals")
