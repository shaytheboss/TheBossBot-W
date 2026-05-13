from sqlalchemy import BigInteger, Column, Integer, Numeric, String, Text, TIMESTAMP
from sqlalchemy.sql import func
from app.database import Base


class Pirep(Base):
    __tablename__ = "pireps"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    near_icao = Column(String(4), nullable=False)
    observed_at = Column(TIMESTAMP(timezone=True), nullable=False)
    location_offset = Column(Text)
    flight_level_ft = Column(Integer)
    aircraft_type = Column(String(10))
    temperature_c = Column(Numeric(5, 1))
    wind_direction = Column(Integer)
    wind_speed_kt = Column(Integer)
    turbulence = Column(String(20))
    icing = Column(String(20))
    raw_pirep = Column(Text)
    ingested_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
