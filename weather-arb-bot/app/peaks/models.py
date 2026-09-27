"""One row per city per local day: the high, and when it came.

Sized for the database budget: ~48 rows a day, REAL not DOUBLE, and the
primary key is the natural (city_id, local_date) — no surrogate id.
"""
from sqlalchemy import Column, Date, Integer, REAL, SmallInteger, String, TIMESTAMP

from app.database import Base


class DailyPeak(Base):
    __tablename__ = "daily_peaks"

    city_id = Column(Integer, primary_key=True, autoincrement=False)
    local_date = Column(Date, primary_key=True)
    icao = Column(String(4), nullable=False)          # the station it was read from
    max_f = Column(REAL, nullable=False)
    # Local decimal hour of the first reading at the day's max, and of the
    # last one (a plateau shows as a gap between the two).
    peak_hour = Column(REAL, nullable=False)
    peak_last_hour = Column(REAL, nullable=False)
    n_obs = Column(SmallInteger, nullable=False)
    # Coverage of the day, so a day with a gap in the afternoon is not
    # mistaken for a day that peaked at noon.
    first_obs_hour = Column(REAL, nullable=False)
    last_obs_hour = Column(REAL, nullable=False)
    max_gap_h = Column(REAL, nullable=False)
    spike_dropped = Column(SmallInteger, nullable=False, default=0)
    computed_at = Column(TIMESTAMP(timezone=True), nullable=False)
