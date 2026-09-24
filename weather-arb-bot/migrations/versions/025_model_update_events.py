"""Record when each Open-Meteo model's numbers change (app/workers/open_meteo_job.py).

One new, small table; no existing table is touched.

Revision ID: 025_model_update_events
Revises: 024_widen_intraday_lock_state
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "025_model_update_events"
down_revision = "024_widen_intraday_lock_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_update_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("detected_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("prev_fetch_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("cities_changed", sa.SmallInteger(), nullable=False),
        sa.Column("cities_compared", sa.SmallInteger(), nullable=False),
    )
    op.create_index("idx_model_update_events_detected", "model_update_events", ["detected_at"])


def downgrade():
    op.drop_index("idx_model_update_events_detected", table_name="model_update_events")
    op.drop_table("model_update_events")
