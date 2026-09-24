"""When a model's numbers actually changed on Open-Meteo.

One row per (source, fetch run) in which at least one city's forecast differed
from that city's previous fetch. `prev_fetch_at` → `detected_at` brackets the
moment the new model run became available, which is what the fetch times of
the slower tiers are tuned against.

The forecasts table cannot answer this: retention de-duplicates it to one row
per made-day after 90 minutes. This table is tiny — tens of rows a day —
and pruned after RETENTION_DAYS by the fetch job itself.
"""
from sqlalchemy import Column, Index, Integer, SmallInteger, String, TIMESTAMP

from app.database import Base


class ModelUpdateEvent(Base):
    __tablename__ = "model_update_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(30), nullable=False)
    detected_at = Column(TIMESTAMP(timezone=True), nullable=False)
    # The latest earlier fetch among the cities that changed: the new run
    # appeared somewhere between the two.
    prev_fetch_at = Column(TIMESTAMP(timezone=True), nullable=False)
    cities_changed = Column(SmallInteger, nullable=False)
    cities_compared = Column(SmallInteger, nullable=False)

    __table_args__ = (Index("idx_model_update_events_detected", "detected_at"),)
