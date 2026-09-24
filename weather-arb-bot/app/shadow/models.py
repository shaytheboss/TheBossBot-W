"""Tables for the shadow study. Separate from every production table.

No foreign keys, on purpose. A research table that referenced markets or
outcomes could block a production delete, or fail an insert while production
is mid-migration. Referential integrity is not worth that trade here: an
orphaned research row is harmless, a blocked production write is not.

Storage is sized deliberately (the database volume is the constraint this
project keeps running into):

  - REAL, not DOUBLE, for probabilities and temperatures. Four bytes is far
    more precision than a market quoted in cents needs.
  - The primary key IS (outcome_id, taken_at). No surrogate id column and no
    second unique index — the pair is already unique by construction, one row
    per bucket per hour.
  - taken_at is indexed with BRIN, not btree. The table is append-only in time
    order, which is exactly the case BRIN exists for: a few kilobytes instead
    of a btree the size of the table's own key column.
"""
from sqlalchemy import (
    Boolean, Column, Date, Index, Integer, REAL, SmallInteger, TIMESTAMP,
)

from app.database import Base


class ShadowSnapshot(Base):
    __tablename__ = "shadow_snapshots"

    outcome_id = Column(Integer, primary_key=True, autoincrement=False)
    # Floored to the hour, so a second run inside the same hour is a no-op
    # rather than a duplicate.
    taken_at = Column(TIMESTAMP(timezone=True), primary_key=True)

    market_id = Column(Integer, nullable=False)
    city_id = Column(Integer, nullable=False)
    event_date = Column(Date, nullable=False)

    # The city's own clock — the axis the analysis is cut on. "20 hours to
    # close" means the same thing in Tokyo and New York; "12:00 UTC" does not.
    hours_to_close = Column(REAL, nullable=False)
    local_hour = Column(SmallInteger, nullable=False)

    # P(YES) for this bucket. model_p is what the detector would act on:
    # normalised across the market when production would normalise, raw
    # otherwise (flagged by `normalized`).
    model_p = Column(REAL, nullable=False)
    raw_p = Column(REAL, nullable=False)
    normalized = Column(Boolean, nullable=False)
    # The market's view. market_p is the LIVE order-book mid when the book was
    # readable (price_live true), otherwise the stored midpoint. bid/ask are
    # what could actually have been traded; NULL when the book was not usable.
    market_p = Column(REAL)
    bid = Column(REAL)
    ask = Column(REAL)
    price_live = Column(Boolean)
    # Minutes since the price job last ran — the freshness stamp that matters
    # for rows that fell back to the stored price.
    price_job_age_min = Column(Integer)

    n_sources = Column(SmallInteger)       # global deterministic sources reporting
    forecast_age_min = Column(Integer)     # minutes since the newest forecast landed
    forecast_high_f = Column(REAL)
    sigma = Column(REAL)

    __table_args__ = (
        Index("ix_shadow_market_time", "market_id", "taken_at"),
        Index("ix_shadow_taken_brin", "taken_at", postgresql_using="brin"),
    )


class ShadowMarketState(Base):
    """One row per market the study has seen: when tracking began, and whether
    the post-resolution summary has gone out, so it is sent exactly once."""
    __tablename__ = "shadow_market_state"

    market_id = Column(Integer, primary_key=True, autoincrement=False)
    first_seen_at = Column(TIMESTAMP(timezone=True), nullable=False)
    summary_sent_at = Column(TIMESTAMP(timezone=True))
