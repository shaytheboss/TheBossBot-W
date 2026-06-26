"""Add virtual_exit table for tracking theoretical stop-loss events.

When a fresh forecast diverges materially from the one used to open a
virtual beta position, we record a theoretical exit here WITHOUT touching
the original opportunity row.  This keeps the existing virtual position
tracking untouched while giving us a clean audit trail of "how much money
the exit system would have saved."

Revision ID: 017_virtual_exit
Revises: 016_opportunity_estimator
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "017_virtual_exit"
down_revision = "016_opportunity_estimator"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "virtual_exits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "opportunity_id",
            sa.Integer(),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "triggered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Price at which we *would* have exited (market bid for YES buys, ask for NO).
        sa.Column("theoretical_exit_price", sa.Numeric(6, 4), nullable=True),
        # Confidence score (0-100) at entry and at the moment the exit was triggered.
        sa.Column("entry_confidence", sa.Integer(), nullable=True),
        sa.Column("exit_confidence", sa.Integer(), nullable=True),
        # How far the fresh forecast shifted vs the entry forecast (°F).
        sa.Column("forecast_shift_f", sa.Numeric(6, 2), nullable=True),
        # Human-readable description of what tripped the exit.
        sa.Column("trigger_reason", sa.Text(), nullable=True),
        # Full beta breakdown dict at the moment of exit for post-hoc analysis.
        sa.Column("signals_at_exit", JSONB(), nullable=True),
        # Theoretical P&L if we had exited at theoretical_exit_price.
        # = virtual_shares * (theoretical_exit_price - virtual_entry_price)
        # Positive = profit, negative = loss avoided.
        sa.Column("theoretical_pnl", sa.Numeric(8, 4), nullable=True),
    )
    op.create_index(
        "ix_virtual_exits_triggered_at",
        "virtual_exits",
        ["triggered_at"],
    )


def downgrade():
    op.drop_index("ix_virtual_exits_triggered_at", "virtual_exits")
    op.drop_table("virtual_exits")
