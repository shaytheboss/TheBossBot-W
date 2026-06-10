"""Intraday subsystem: intraday_opportunities table + cities.intraday_enabled.

Completely separate from the daily `opportunities` table so the two
strategies can never corrupt each other's tracking.

Revision ID: 011_intraday
Revises: 010_add_city_suspension
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "011_intraday"
down_revision = "010_add_city_suspension"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "intraday_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("outcome_id", sa.Integer(), sa.ForeignKey("market_outcomes.id"), nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("side", sa.String(3), nullable=False),
        sa.Column("market_price", sa.Numeric(6, 4), nullable=False),
        sa.Column("estimated_true_prob", sa.Numeric(6, 4), nullable=False),
        sa.Column("edge", sa.Numeric(6, 4), nullable=False),
        sa.Column("confidence_score", sa.Integer(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("alert_sent", sa.Boolean(), default=False),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        # ── intraday-specific context (the learning loop) ──
        sa.Column("local_hour", sa.Float(), nullable=True),
        sa.Column("hours_to_peak_end", sa.Float(), nullable=True),
        sa.Column("running_max_f", sa.Float(), nullable=True),
        sa.Column("expected_final_max_f", sa.Float(), nullable=True),
        sa.Column("sigma_used", sa.Float(), nullable=True),
        sa.Column("lock_state", sa.String(20), nullable=True),
        # ── virtual position (mirrors daily table semantics) ──
        sa.Column("virtual_shares", sa.Integer(), nullable=True),
        sa.Column("virtual_entry_price", sa.Float(), nullable=True),
        sa.Column("virtual_cost", sa.Float(), nullable=True),
        sa.Column("virtual_payout", sa.Float(), nullable=True),
        sa.Column("virtual_pnl", sa.Float(), nullable=True),
        sa.Column("virtual_status", sa.String(16), nullable=True),
    )
    op.create_index(
        "ix_intraday_opps_outcome_id", "intraday_opportunities", ["outcome_id"]
    )
    op.create_index(
        "ix_intraday_opps_detected_at", "intraday_opportunities", ["detected_at"]
    )
    op.add_column(
        "cities",
        sa.Column(
            "intraday_enabled", sa.Boolean(), nullable=False,
            server_default="true", default=True,
        ),
    )


def downgrade():
    op.drop_column("cities", "intraday_enabled")
    op.drop_index("ix_intraday_opps_detected_at", table_name="intraday_opportunities")
    op.drop_index("ix_intraday_opps_outcome_id", table_name="intraday_opportunities")
    op.drop_table("intraday_opportunities")
