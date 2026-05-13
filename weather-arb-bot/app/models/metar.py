from sqlalchemy import BigInteger, Column, Index, Integer, Numeric, String, Text, TIMESTAMP, UniqueConstraint
from app.database import Base


class MetarObservation(Base):
    __tablename__ = "metar_observations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    icao = Column(String(4), nullable=False)
    observed_at = Column(TIMESTAMP(timezone=True), nullable=False)
    temperature_f = Column(Numeric(5, 1))
    dew_point_f = Column(Numeric(5, 1))
    humidity_pct = Column(Integer)
    wind_direction = Column(Integer)
    wind_speed_kt = Column(Integer)
    wind_gust_kt = Column(Integer)
    pressure_hg = Column(Numeric(6, 2))
    visibility_sm = Column(Numeric(5, 1))
    conditions = Column(Text)
    raw_metar = Column(Text)

    __table_args__ = (
        UniqueConstraint("icao", "observed_at", name="uq_metar_icao_time"),
        Index("idx_metar_icao_time", "icao", "observed_at"),
    )
