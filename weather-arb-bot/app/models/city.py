from sqlalchemy import Boolean, Column, Integer, Numeric, String, Text, TIMESTAMP
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class City(Base):
    __tablename__ = "cities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    primary_icao = Column(String(4), nullable=False)
    reference_icao = Column(String(4))
    wunderground_url = Column(Text, nullable=False)
    nws_lat = Column(Numeric(7, 4))
    nws_lon = Column(Numeric(7, 4))
    timezone = Column(String(50), nullable=False)
    buoy_id = Column(String(10))
    active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    markets = relationship("Market", back_populates="city")
    alerts = relationship("Alert", back_populates="city")
