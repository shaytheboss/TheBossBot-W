from sqlalchemy import Column, ForeignKey, Integer, Numeric, String, Text, TIMESTAMP, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class Opportunity(Base):
    __tablename__ = "opportunities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    outcome_id = Column(Integer, ForeignKey("market_outcomes.id"), nullable=False)
    detected_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    side = Column(String(3), nullable=False)
    market_price = Column(Numeric(6, 4), nullable=False)
    estimated_true_prob = Column(Numeric(6, 4), nullable=False)
    edge = Column(Numeric(6, 4), nullable=False)
    confidence_score = Column(Integer, nullable=False)
    signals = Column(JSONB, nullable=False)
    alert_sent = Column(Boolean, default=False)
    closed_at = Column(TIMESTAMP(timezone=True))
    outcome = Column(Text)

    outcome_ref = relationship("MarketOutcome", back_populates="opportunities")
    alerts = relationship("Alert", back_populates="opportunity")
