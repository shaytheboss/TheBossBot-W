from sqlalchemy import (
    BigInteger, Boolean, Column, Date, Decimal, ForeignKey,
    Integer, Numeric, String, Text, TIMESTAMP, UniqueConstraint
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship, Index
from app.database import Base


class Market(Base):
    __tablename__ = "markets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("cities.id"))
    external_id = Column(String(100), unique=True, nullable=False)
    platform = Column(String(20), default="polymarket")
    question = Column(Text, nullable=False)
    resolution_source = Column(Text)
    event_date = Column(Date, nullable=False)
    resolution_time = Column(TIMESTAMP(timezone=True))
    resolved = Column(Boolean, default=False)
    resolution_value = Column(Text)

    city = relationship("City", back_populates="markets")
    outcomes = relationship("MarketOutcome", back_populates="market")
    alerts = relationship("Alert", back_populates="market")


class MarketOutcome(Base):
    __tablename__ = "market_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    bucket_label = Column(String(50), nullable=False)
    bucket_min = Column(Integer)
    bucket_max = Column(Integer)

    market = relationship("Market", back_populates="outcomes")
    prices = relationship("MarketPrice", back_populates="outcome")
    opportunities = relationship("Opportunity", back_populates="outcome")

    __table_args__ = (
        UniqueConstraint("market_id", "bucket_label", name="uq_outcome_market_bucket"),
    )


class MarketPrice(Base):
    __tablename__ = "market_prices"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    outcome_id = Column(Integer, ForeignKey("market_outcomes.id"), nullable=False)
    timestamp = Column(TIMESTAMP(timezone=True), nullable=False)
    yes_price = Column(Numeric(6, 4), nullable=False)
    no_price = Column(Numeric(6, 4), nullable=False)
    volume_24h = Column(Numeric(18, 2))

    outcome = relationship("MarketOutcome", back_populates="prices")

    __table_args__ = (
        UniqueConstraint("outcome_id", "timestamp", name="uq_price_outcome_time"),
        Index("idx_prices_outcome_time", "outcome_id", "timestamp"),
    )
