"""Record weather.gov station observations for US cities (app/nwsobs/).

One new, small table; no existing table is touched.

Revision ID: 031_nws_observations
Revises: 030_model_run_values
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "031_nws_observations"
down_revision = "030_model_run_values"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nws_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("icao", sa.String(4), nullable=False),
        sa.Column("observed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("temperature_c", sa.REAL(), nullable=True),
        sa.Column("is_metar", sa.Boolean(), nullable=False),
        sa.Column("qc", sa.String(2), nullable=True),
        sa.Column("fetched_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("icao", "observed_at", name="uq_nws_obs_icao_time"),
    )
    op.create_index("idx_nws_obs_observed", "nws_observations", ["observed_at"])


def downgrade():
    op.drop_index("idx_nws_obs_observed", table_name="nws_observations")
    op.drop_table("nws_observations")
