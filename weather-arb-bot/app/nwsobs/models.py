"""One row per weather.gov observation of a US station."""
from sqlalchemy import Boolean, Column, Index, Integer, REAL, String, TIMESTAMP, UniqueConstraint

from app.database import Base


class NwsObservation(Base):
    __tablename__ = "nws_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    icao = Column(String(4), nullable=False)
    observed_at = Column(TIMESTAMP(timezone=True), nullable=False)
    # As served: whole °C on the five-minute readings, tenths on the METAR.
    temperature_c = Column(REAL, nullable=True)
    # True for the hourly METAR / SPECI (it carries a rawMessage), False for
    # the five-minute readings in between.
    is_metar = Column(Boolean, nullable=False)
    qc = Column(String(2), nullable=True)
    fetched_at = Column(TIMESTAMP(timezone=True), nullable=False)

    __table_args__ = (
        # One reading per station and minute; also the index the per-station
        # reads use.
        UniqueConstraint("icao", "observed_at", name="uq_nws_obs_icao_time"),
        Index("idx_nws_obs_observed", "observed_at"),
    )
