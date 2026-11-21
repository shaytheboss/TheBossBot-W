"""One row per bucket and side at most: the first moment the bot's signal met
the real-track rule, and what buying it right then cost."""
from sqlalchemy import Boolean, Column, Date, Index, Integer, REAL, SmallInteger, String, TIMESTAMP, UniqueConstraint

from app.database import Base


class RealTrackBuy(Base):
    __tablename__ = "realtrack_buys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    estimator = Column(String(8), nullable=False)      # alpha | beta
    city_id = Column(Integer, nullable=False)
    market_id = Column(Integer, nullable=False)
    outcome_id = Column(Integer, nullable=False)
    side = Column(String(3), nullable=False)           # YES | NO
    event_date = Column(Date, nullable=False)
    bought_at = Column(TIMESTAMP(timezone=True), nullable=False)
    local_hour = Column(REAL, nullable=False)
    confidence = Column(SmallInteger, nullable=False)  # the bot's %, as stored on its own signals
    # The live book at that moment. price is what one share of `side` cost:
    # the YES ask for YES, 1 - the YES bid for NO.
    bid = Column(REAL, nullable=False)
    ask = Column(REAL, nullable=False)
    price = Column(REAL, nullable=False)
    shares = Column(SmallInteger, nullable=False)
    # A research position on this bucket and side was already open, so the
    # research track recorded nothing new here — this row is the same-day entry.
    research_open = Column(Boolean, nullable=False, default=False)
    alerted = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("outcome_id", "side", name="uq_realtrack_outcome_side"),
        Index("idx_realtrack_bought_at", "bought_at"),
    )
