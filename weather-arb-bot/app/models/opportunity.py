from sqlalchemy import Column, Float, ForeignKey, Integer, Numeric, String, Text, TIMESTAMP, Boolean
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

    # ── Virtual position fields (populated when confidence >= buy threshold) ──
    # All nullable so legacy rows pre-dating this feature remain valid.
    virtual_shares = Column(Integer, nullable=True, default=None)
    # Per-share entry price in 0-1 range (the ASK paid for the bet's SIDE).
    # For YES bets this is book["ask"]; for NO bets this is 1 - book["bid"].
    virtual_entry_price = Column(Float, nullable=True, default=None)
    virtual_cost = Column(Float, nullable=True, default=None)
    virtual_payout = Column(Float, nullable=True, default=None)
    virtual_pnl = Column(Float, nullable=True, default=None)
    # One of: "open", "win", "loss" (or NULL when no virtual buy was made).
    virtual_status = Column(String(16), nullable=True, default=None)

    # Which estimator produced this row: "alpha" (classic) or "beta" (calibrated).
    # NULL on legacy rows — treated as "alpha" in all display logic.
    estimator = Column(String(8), nullable=True, default="alpha")

    outcome_ref = relationship("MarketOutcome", back_populates="opportunities")
    alerts = relationship("Alert", back_populates="opportunity")
