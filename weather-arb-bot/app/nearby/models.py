"""One row per market at most: the moment the nearby stations said the
settlement station's high is in, and what it would have cost to buy then."""
from sqlalchemy import JSON, Boolean, Column, Date, Index, Integer, REAL, SmallInteger, String, TIMESTAMP, UniqueConstraint

from app.database import Base


class NearbySignal(Base):
    __tablename__ = "nearby_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, nullable=False)
    market_id = Column(Integer, nullable=False)
    outcome_id = Column(Integer, nullable=False)       # the bucket holding the max
    event_date = Column(Date, nullable=False)
    signaled_at = Column(TIMESTAMP(timezone=True), nullable=False)   # the tradable moment (or the first sighting)
    # When the signal first fired, tradable or not: signaled_at - first_seen_at
    # is how long the book took to allow the buy.
    first_seen_at = Column(TIMESTAMP(timezone=True))
    local_hour = Column(REAL, nullable=False)
    icao = Column(String(4), nullable=False)
    max_f = Column(REAL, nullable=False)              # settlement station's high so far
    current_f = Column(REAL, nullable=False)
    n_nearby = Column(SmallInteger, nullable=False)
    n_falling = Column(SmallInteger, nullable=False)
    detail = Column(JSON)                             # [{icao, km, now_f, high_f, below_f}]
    # The real book at that moment — what a paper buy would have paid.
    bid = Column(REAL)
    ask = Column(REAL)
    ask_size = Column(REAL)
    # Could the paper buy actually have been made at that ask (signal.tradable)?
    # Only tradable signals are announced and counted in the result.
    tradable = Column(Boolean)
    # The mid 10 and 30 minutes later: how fast the market caught up.
    mid_10 = Column(REAL)
    mid_30 = Column(REAL)
    alerted = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("market_id", name="uq_nearby_signal_market"),
        Index("idx_nearby_signal_at", "signaled_at"),
    )
