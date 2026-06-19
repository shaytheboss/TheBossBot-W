"""Add estimator column to opportunities — tracks alpha vs beta rows.

Each opportunity row is now tagged with the estimator that produced it:
  "alpha" = classic probability_estimator.py (unchanged)
  "beta"  = new calibrated beta_estimator.py (per-city bias, MAE-sigma, etc.)

Both run in parallel. NULL rows (legacy data) are treated as "alpha".

Revision ID: 016_opportunity_estimator
Revises: 015_collector_miss
"""
import sqlalchemy as sa
from alembic import op

revision = "016_opportunity_estimator"
down_revision = "015_collector_miss"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "opportunities",
        sa.Column(
            "estimator",
            sa.String(8),
            nullable=True,
            server_default="alpha",
        ),
    )
    op.create_index(
        "ix_opportunities_estimator",
        "opportunities",
        ["estimator"],
    )


def downgrade():
    op.drop_index("ix_opportunities_estimator", "opportunities")
    op.drop_column("opportunities", "estimator")
