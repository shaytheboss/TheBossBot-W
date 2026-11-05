"""Record each Open-Meteo model's forecast high whenever it changes
(app/models/model_run_value.py).

One new, small table; no existing table is touched.

Revision ID: 030_model_run_values
Revises: 029_shadow_v2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "030_model_run_values"
down_revision = "029_shadow_v2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_run_values",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("forecast_for_date", sa.Date(), nullable=False),
        sa.Column("detected_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("prev_fetch_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("high_f", sa.REAL(), nullable=False),
    )
    op.create_index("idx_model_run_values_detected", "model_run_values", ["detected_at"])


def downgrade():
    op.drop_index("idx_model_run_values_detected", table_name="model_run_values")
    op.drop_table("model_run_values")
