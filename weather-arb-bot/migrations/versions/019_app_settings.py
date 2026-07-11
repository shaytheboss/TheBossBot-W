"""Add app_settings key-value table for persistent runtime settings.

The admin Settings tab previously mutated the in-memory Settings object only —
every deploy/restart silently reverted all thresholds to config defaults
(e.g. the user sets alert threshold 0.80, a redeploy brings back the 0.75
default, and sub-80% alerts reappear). This table persists the overrides.

Revision ID: 019_app_settings
Revises: 018_hong_kong_coords
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "019_app_settings"
down_revision = "018_hong_kong_coords"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_table("app_settings")
