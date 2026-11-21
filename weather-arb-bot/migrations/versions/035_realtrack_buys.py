"""The real track (app/realtrack): same-day, high-confidence signals as
virtual buys at the live price.

One new, small table — at most one row per bucket and side (a few a day);
no existing table is touched.

Revision ID: 035_realtrack_buys
Revises: 034_nearby_first_seen
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "035_realtrack_buys"
down_revision = "034_nearby_first_seen"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "realtrack_buys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("estimator", sa.String(8), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("outcome_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(3), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("bought_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("local_hour", sa.REAL(), nullable=False),
        sa.Column("confidence", sa.SmallInteger(), nullable=False),
        sa.Column("bid", sa.REAL(), nullable=False),
        sa.Column("ask", sa.REAL(), nullable=False),
        sa.Column("price", sa.REAL(), nullable=False),
        sa.Column("shares", sa.SmallInteger(), nullable=False),
        sa.Column("research_open", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("alerted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("outcome_id", "side", name="uq_realtrack_outcome_side"),
    )
    op.create_index("idx_realtrack_bought_at", "realtrack_buys", ["bought_at"])


def downgrade():
    op.drop_index("idx_realtrack_bought_at", table_name="realtrack_buys")
    op.drop_table("realtrack_buys")
