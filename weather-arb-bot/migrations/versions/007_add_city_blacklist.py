"""Add blacklisted flag to cities.

Blacklisted cities still generate Telegram alerts but never open a virtual-buy
position — no simulated money is committed regardless of confidence.

Revision ID: 007_add_city_blacklist
Revises: 006_forecast_accuracy
"""
from alembic import op
import sqlalchemy as sa

revision = "007_add_city_blacklist"
down_revision = "006_forecast_accuracy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cities",
        sa.Column(
            "blacklisted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("cities", "blacklisted")
