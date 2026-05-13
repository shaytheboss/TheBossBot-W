"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-24 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("primary_icao", sa.String(4), nullable=False),
        sa.Column("reference_icao", sa.String(4)),
        sa.Column("wunderground_url", sa.Text(), nullable=False),
        sa.Column("nws_lat", sa.Numeric(7, 4)),
        sa.Column("nws_lon", sa.Numeric(7, 4)),
        sa.Column("timezone", sa.String(50), nullable=False),
        sa.Column("buoy_id", sa.String(10)),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "metar_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("icao", sa.String(4), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("temperature_f", sa.Numeric(5, 1)),
        sa.Column("dew_point_f", sa.Numeric(5, 1)),
        sa.Column("humidity_pct", sa.Integer()),
        sa.Column("wind_direction", sa.Integer()),
        sa.Column("wind_speed_kt", sa.Integer()),
        sa.Column("wind_gust_kt", sa.Integer()),
        sa.Column("pressure_hg", sa.Numeric(6, 2)),
        sa.Column("visibility_sm", sa.Numeric(5, 1)),
        sa.Column("conditions", sa.Text()),
        sa.Column("raw_metar", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("icao", "observed_at", name="uq_metar_icao_time"),
    )
    op.create_index("idx_metar_icao_time", "metar_observations", ["icao", "observed_at"])

    op.create_table(
        "forecasts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("forecast_for_date", sa.Date(), nullable=False),
        sa.Column("predicted_high_f", sa.Integer()),
        sa.Column("predicted_low_f", sa.Integer()),
        sa.Column("conditions", sa.Text()),
        sa.Column("raw_data", postgresql.JSONB()),
        sa.Column(
            "retrieved_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_forecast_city_date",
        "forecasts",
        ["city_id", "forecast_for_date", "source"],
    )

    op.create_table(
        "pireps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("near_icao", sa.String(4), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("location_offset", sa.Text()),
        sa.Column("flight_level_ft", sa.Integer()),
        sa.Column("aircraft_type", sa.String(10)),
        sa.Column("temperature_c", sa.Numeric(5, 1)),
        sa.Column("wind_direction", sa.Integer()),
        sa.Column("wind_speed_kt", sa.Integer()),
        sa.Column("turbulence", sa.String(20)),
        sa.Column("icing", sa.String(20)),
        sa.Column("raw_pirep", sa.Text()),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "markets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("cities.id")),
        sa.Column("external_id", sa.String(100), unique=True, nullable=False),
        sa.Column("platform", sa.String(20), server_default=sa.text("'polymarket'")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("resolution_source", sa.Text()),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("resolution_time", sa.TIMESTAMP(timezone=True)),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("resolution_value", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "market_outcomes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("bucket_label", sa.String(50), nullable=False),
        sa.Column("bucket_min", sa.Integer()),
        sa.Column("bucket_max", sa.Integer()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_id", "bucket_label", name="uq_outcome_market_bucket"),
    )

    op.create_table(
        "market_prices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "outcome_id", sa.Integer(), sa.ForeignKey("market_outcomes.id"), nullable=False
        ),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("yes_price", sa.Numeric(6, 4), nullable=False),
        sa.Column("no_price", sa.Numeric(6, 4), nullable=False),
        sa.Column("volume_24h", sa.Numeric(18, 2)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outcome_id", "timestamp", name="uq_price_outcome_time"),
    )
    op.create_index("idx_prices_outcome_time", "market_prices", ["outcome_id", "timestamp"])

    op.create_table(
        "opportunities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "outcome_id", sa.Integer(), sa.ForeignKey("market_outcomes.id"), nullable=False
        ),
        sa.Column(
            "detected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column("side", sa.String(3), nullable=False),
        sa.Column("market_price", sa.Numeric(6, 4), nullable=False),
        sa.Column("estimated_true_prob", sa.Numeric(6, 4), nullable=False),
        sa.Column("edge", sa.Numeric(6, 4), nullable=False),
        sa.Column("confidence_score", sa.Integer(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("alert_sent", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("outcome", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alert_type", sa.String(30), nullable=False),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("cities.id")),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id")),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("opportunities.id")),
        sa.Column("priority", sa.String(10)),
        sa.Column(
            "sent_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column("message_text", sa.Text()),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "telegram_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("username", sa.String(50)),
        sa.Column("cities_watched", postgresql.ARRAY(sa.Integer())),
        sa.Column("min_confidence", sa.Integer(), server_default=sa.text("60")),
        sa.Column("alert_types_enabled", postgresql.ARRAY(sa.Text())),
        sa.Column("quiet_hours_start", sa.Time()),
        sa.Column("quiet_hours_end", sa.Time()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("telegram_users")
    op.drop_table("alerts")
    op.drop_table("opportunities")
    op.drop_index("idx_prices_outcome_time", "market_prices")
    op.drop_table("market_prices")
    op.drop_table("market_outcomes")
    op.drop_table("markets")
    op.drop_table("pireps")
    op.drop_index("idx_forecast_city_date", "forecasts")
    op.drop_table("forecasts")
    op.drop_index("idx_metar_icao_time", "metar_observations")
    op.drop_table("metar_observations")
    op.drop_table("cities")
