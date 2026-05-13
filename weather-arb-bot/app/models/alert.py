from sqlalchemy import (
    BigInteger, Column, ForeignKey, Integer, String, Text, TIMESTAMP,
    ARRAY, Time, Boolean
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    alert_type = Column(String(30), nullable=False)
    city_id = Column(Integer, ForeignKey("cities.id"))
    market_id = Column(Integer, ForeignKey("markets.id"))
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"))
    priority = Column(String(10))
    sent_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    message_text = Column(Text)
    telegram_message_id = Column(BigInteger)

    city = relationship("City", back_populates="alerts")
    market = relationship("Market", back_populates="alerts")
    opportunity = relationship("Opportunity", back_populates="alerts")


class TelegramUser(Base):
    __tablename__ = "telegram_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(50))
    cities_watched = Column(PG_ARRAY(Integer), default=[])
    min_confidence = Column(Integer, default=60)
    alert_types_enabled = Column(PG_ARRAY(String), default=[])
    quiet_hours_start = Column(Time)
    quiet_hours_end = Column(Time)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
