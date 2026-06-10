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
    # Blacklisted cities still generate Telegram alerts (so we keep tracking the
    # opportunity and learning from it), but never open a virtual-buy position —
    # no simulated money is committed regardless of how high the confidence is.
    # Use for cities whose markets we don't trust enough to bet on yet.
    blacklisted = Column(Boolean, default=False, nullable=False, server_default="false")
    # Compass bearing (0-359°) of the onshore (sea→land) wind for this city,
    # e.g. ~300 for LA, ~110 for Miami. Drives the sea-breeze heuristics in the
    # probability estimator / confidence scorer. NULL disables those heuristics
    # for the city — a wrong direction is worse than no adjustment.
    onshore_wind_dir = Column(Integer, nullable=True, default=None)
    # Temporary suspension: city continues to alert but won't open new virtual
    # positions until suspended_until expires. Auto-set after N consecutive
    # high-confidence losses; auto-clears when the timestamp passes.
    suspended_until = Column(TIMESTAMP(timezone=True), nullable=True, default=None)
    suspension_reason = Column(Text, nullable=True, default=None)
    polymarket_slug = Column(String(60))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    markets = relationship("Market", back_populates="city")
    alerts = relationship("Alert", back_populates="city")
