import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.analyzers.signal_aggregator import SignalAggregator
from app.analyzers.probability_estimator import estimate_true_probability
from app.analyzers.confidence_scorer import compute_confidence
from app.config import settings
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity

logger = logging.getLogger(__name__)

aggregator = SignalAggregator()

MIN_CONFIDENCE = 80  # hard floor — overrides settings so it cannot be lowered accidentally


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

        for outcome in outcomes:
            try:
                opp = await _analyze_outcome(db, city, outcome)
                if opp:
                    found.append(opp)
            except Exception as e:
                logger.error(f"Error analyzing outcome {outcome.id}: {e}", exc_info=True)

    return found


async def _analyze_outcome(db: AsyncSession, city: City, outcome: MarketOutcome) -> Optional[Opportunity]:
    signals = await aggregator.aggregate(
        db=db, city_id=city.id, primary_icao=city.primary_icao,
        reference_icao=city.reference_icao, outcome=outcome,
    )

    price_info = signals.get("market_price")
    if not price_info:
        return None

    yes_price = price_info["yes_price"]
    true_prob = estimate_true_probability(signals, outcome.bucket_min, outcome.bucket_max)
    confidence = compute_confidence(signals, outcome.bucket_min, outcome.bucket_max)

    # Only flag when the market is UNDERPRICING the outcome relative to our model.
    # i.e. we think YES is more likely than what the market charges.
    # Never fire on the NO side — shorting a weather bucket is not our use-case.
    yes_edge = true_prob - yes_price

    min_conf = max(MIN_CONFIDENCE, settings.min_confidence_for_alert)
    if yes_edge < settings.min_edge_for_alert or confidence < min_conf:
        return None

    opp = Opportunity(
        outcome_id=outcome.id,
        detected_at=datetime.now(timezone.utc),
        side="YES",
        market_price=yes_price,
        estimated_true_prob=true_prob,
        edge=yes_edge,
        confidence_score=confidence,
        signals=signals,
        alert_sent=False,
    )
    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return opp
