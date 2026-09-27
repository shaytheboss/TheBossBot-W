"""daily_peaks: when each city's daily high arrived (app/peaks/). Record-only.

One new table, ~48 rows a day, natural primary key (city_id, local_date), no
foreign keys — a research table must never block a production write.

Revision ID: 027_daily_peaks
Revises: 026_shadow_intraday_p
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "027_daily_peaks"
down_revision = "026_shadow_intraday_p"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "daily_peaks",
        sa.Column("city_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("icao", sa.String(4), nullable=False),
        sa.Column("max_f", sa.REAL(), nullable=False),
        sa.Column("peak_hour", sa.REAL(), nullable=False),
        sa.Column("peak_last_hour", sa.REAL(), nullable=False),
        sa.Column("n_obs", sa.SmallInteger(), nullable=False),
        sa.Column("first_obs_hour", sa.REAL(), nullable=False),
        sa.Column("last_obs_hour", sa.REAL(), nullable=False),
        sa.Column("max_gap_h", sa.REAL(), nullable=False),
        sa.Column("spike_dropped", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("computed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("city_id", "local_date"),
    )


def downgrade():
    op.drop_table("daily_peaks")
