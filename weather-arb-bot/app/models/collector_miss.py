from sqlalchemy import Column, Date, ForeignKey, Index, Integer, String, TIMESTAMP, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class CollectorMiss(Base):
    """Records when a weather data source returns no data for a city/date pair.

    Written by the daily opportunity detector whenever estimate_with_breakdown
    reports missing_sources or missing_no_key in its breakdown. Over time this
    reveals systematic gaps: e.g. "ECMWF never responds for Tokyo" or "Tomorrow.io
    is unconfigured" — which helps prioritise which API keys to add.

    One row per (city_id, event_date, source, miss_reason). Duplicate inserts on
    the same (city, date, source) are silently ignored via INSERT OR IGNORE so
    running the detector multiple times on the same day doesn't bloat the table.
    """
    __tablename__ = "collector_miss"
    __table_args__ = (
        UniqueConstraint(
            "city_id", "event_date", "source", "miss_reason",
            name="uq_collector_miss_city_date_source",
        ),
        Index("ix_collector_miss_city_date", "city_id", "event_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False)
    event_date = Column(Date, nullable=False)
    source = Column(String(32), nullable=False)
    # "no_data" = key configured but API returned nothing
    # "no_key"  = API key not configured in settings
    miss_reason = Column(String(16), nullable=False, default="no_data")
    detected_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
