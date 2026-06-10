"""Add suspended_until and suspension_reason columns to cities.

Smart suspension replaces the binary blacklist for temporary cooling-off
periods after consecutive high-confidence losses. When suspended_until is
set to a future datetime the city continues to generate Telegram alerts
(tracking is preserved) but no new virtual-buy positions are opened.
The suspension expires automatically — no manual intervention needed.

Revision ID: 010_add_city_suspension
Revises: 009_add_city_onshore_dir
"""
import sqlalchemy as sa
from alembic import op

revision = "010_add_city_suspension"
down_revision = "009_add_city_onshore_dir"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cities",
        sa.Column("suspended_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "cities",
        sa.Column("suspension_reason", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("cities", "suspension_reason")
    op.drop_column("cities", "suspended_until")
