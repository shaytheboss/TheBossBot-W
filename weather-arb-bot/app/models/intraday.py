from sqlalchemy import Column, Float, ForeignKey, Integer, Numeric, String, Text, TIMESTAMP, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class IntradayOpportunity(Base):
    """Short-horizon (same-day, hours-scale) opportunity.

    Deliberately a SEPARATE table from `opportunities` — the intraday strategy
    has its own thresholds, its own probability model and its own learning
    loop, and must never contaminate the daily bot's tracking.
    """
    __tablename__ = "intraday_opportunities"

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

    # ── Intraday context, recorded at detection time for the learning loop ──
    local_hour = Column(Float, nullable=True)            # city-local decimal hour
    hours_to_peak_end = Column(Float, nullable=True)
    running_max_f = Column(Float, nullable=True)         # METAR max so far today
    expected_final_max_f = Column(Float, nullable=True)  # model's μ
    sigma_used = Column(Float, nullable=True)
    # None | "yes_locked" (floor already touched) | "yes_impossible" (max above bucket)
    lock_state = Column(String(20), nullable=True)

    # ── Virtual position (same semantics as the daily table) ──
    virtual_shares = Column(Integer, nullable=True, default=None)
    virtual_entry_price = Column(Float, nullable=True, default=None)
    virtual_cost = Column(Float, nullable=True, default=None)
    virtual_payout = Column(Float, nullable=True, default=None)
    virtual_pnl = Column(Float, nullable=True, default=None)
    virtual_status = Column(String(16), nullable=True, default=None)

    outcome_ref = relationship("MarketOutcome")
