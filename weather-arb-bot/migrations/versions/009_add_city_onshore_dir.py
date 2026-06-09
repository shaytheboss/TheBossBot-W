"""Add onshore_wind_dir to cities.

Compass bearing (0-359°) of the onshore (sea→land) wind for the city. The
sea-breeze heuristics in the probability estimator and confidence scorer only
run when this is configured — previously the direction was hardcoded to the
US west coast (270-340°) and acted in the WRONG direction for east-coast and
international cities. NULL disables the heuristics for the city.

Revision ID: 009_add_city_onshore_dir
Revises: 008_add_outcome_won
"""
from alembic import op
import sqlalchemy as sa

revision = "009_add_city_onshore_dir"
down_revision = "008_add_outcome_won"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cities",
        sa.Column("onshore_wind_dir", sa.Integer(), nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("cities", "onshore_wind_dir")
