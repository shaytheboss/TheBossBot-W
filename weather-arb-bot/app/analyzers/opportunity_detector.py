import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.analyzers.signal_aggregator import SignalAggregator
from app.analyzers.probability_estimator import estimate_true_probability
from app.config import settings
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity

logger = logging.getLogger(__name__)

aggregator = SignalAggregator()

MIN_PREDICTION_CERTAINTY = 80  # default — overridden by settings.min_confidence_for_alert


async def detect_opportunities(db: AsyncSession) -> List[Opportunity]:
    found: List[Opportunity] = []

    result = await db.execute(
        select(Market).where(Market.resolved == False).order_by(Market.event_date)
    )
    markets: List[Market] = result.scalars().all()

    for market in markets:
        city_result = await db.execute(select(City).where(City.id == market.city_id))
        city: Optional[City] = city_result.scalar_one_or_none()
        if not city:
            continue

        outcomes_result = await db.execute(
            select(MarketOutcome).where(MarketOutcome.market_id == market.id)
        )
        outcomes: List[MarketOutcome] = outcomes_result.scalars().all()

        is_low_market = "lowest" in (market.question or "").lower()

        for outcome in outcomes:
            try:
                opp = await _analyze_outcome(db, city, outcome, market, is_low_market)
                if opp:
                    found.append(opp)
            except Exception as e:
                logger.error(f"Error analyzing outcome {outcome.id}: {e}", exc_info=True)

    return found


async def _analyze_outcome(
    db: AsyncSession,
    city: City,
    outcome: MarketOutcome,
    market: Market,
    is_low_market: bool,
) -> Optional[Opportunity]:
    """Detect an opportunity for a single bucket.

    Gate: our directional certainty = max(true_prob, 1 - true_prob) must be
    >= settings.min_confidence_for_alert percent, AND the edge for that side
    must be >= settings.min_edge_for_alert.

    side=YES when true_prob >= 0.5 (market underprices YES).
    side=NO  when true_prob <  0.5 (market underprices NO).
    """
    signals = await aggregator.aggregate(
        db=db,
        city_id=city.id,
        primary_icao=city.primary_icao,
        reference_icao=city.reference_icao,
        outcome=outcome,
        forecast_date=market.event_date,
        is_low_market=is_low_market,
    )

    price_info = signals.get("market_price")
    if not price_info:
        return None

    yes_price = price_info["yes_price"]
    true_prob = estimate_true_probability(signals, outcome.bucket_min, outcome.bucket_max)

    if true_prob >= 0.5:
        side = "YES"
        certainty = true_prob
        market_implied = yes_price
    else:
        side = "NO"
        certainty = 1.0 - true_prob
        market_implied = 1.0 - yes_price

    edge = certainty - market_implied

    min_certainty = max(0.0, min(1.0, settings.min_confidence_for_alert / 100.0))
    if certainty < min_certainty:
        return None
    if edge < settings.min_edge_for_alert:
        return None

    opp = Opportunity(
        outcome_id=outcome.id,
        detected_at=datetime.now(timezone.utc),
        side=side,
        market_price=yes_price,
        estimated_true_prob=true_prob,
        edge=edge,
        confidence_score=int(round(certainty * 100)),
        signals=signals,
        alert_sent=False,
    )
    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return opp
