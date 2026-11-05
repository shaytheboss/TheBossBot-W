"""Each Open-Meteo model's forecast high for each city, every time it changes.

The forecasts table cannot answer "which run of ICON is most accurate in
Seoul" or "how many minutes does the market take to react to a new run":
retention de-duplicates it to one row per made-day, and model_update_events
says only WHEN a model changed, not what it said. This table keeps the value
itself — written only when the unrounded high for a (source, city, date)
differs from the last fetch, and only for the dates trading can touch
(yesterday to two days ahead). On the order of ten thousand small rows a day,
pruned after RETENTION_DAYS by the fetch job. Research only: nothing in
trading reads it.

`prev_fetch_at` brackets when the new run appeared (between it and
`detected_at`). It is NULL on the first value seen after a restart — a
baseline, not a change.
"""
from sqlalchemy import Column, Date, Index, Integer, REAL, String, TIMESTAMP

from app.database import Base


class ModelRunValue(Base):
    __tablename__ = "model_run_values"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(30), nullable=False)
    city_id = Column(Integer, nullable=False)
    forecast_for_date = Column(Date, nullable=False)
    detected_at = Column(TIMESTAMP(timezone=True), nullable=False)
    prev_fetch_at = Column(TIMESTAMP(timezone=True), nullable=True)
    high_f = Column(REAL, nullable=False)

    __table_args__ = (Index("idx_model_run_values_detected", "detected_at"),)
