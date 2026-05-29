"""Add forecast_accuracy table for learned model error statistics.

Revision ID: 006_forecast_accuracy
Revises: 005_add_bucket_unit
"""
from alembic import op
import sqlalchemy as sa

revision = "006_forecast_accuracy"
down_revision = "005_add_bucket_unit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_accuracy",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("city_id", sa.Integer(), nullable=False, index=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("mae", sa.Float(), nullable=True),
        sa.Column("bias", sa.Float(), nullable=True),
        sa.Column("sigma_estimate", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), default=0),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "city_id", "source", "lead_time_days", "month",
            name="uq_forecast_accuracy_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("forecast_accuracy")
