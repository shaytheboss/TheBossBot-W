"""Add won flag to market_outcomes.

Records which bucket Polymarket settled as the winner so the dashboard can
score each weather model's forecast against the actual result. NULL while the
market is unresolved; True/False once resolved.

Revision ID: 008_add_outcome_won
Revises: 007_add_city_blacklist
"""
from alembic import op
import sqlalchemy as sa

revision = "008_add_outcome_won"
down_revision = "007_add_city_blacklist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "market_outcomes",
        sa.Column("won", sa.Boolean(), nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("market_outcomes", "won")
