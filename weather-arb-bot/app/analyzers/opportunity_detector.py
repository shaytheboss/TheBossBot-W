import logging
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.analyzers.signal_aggregator import SignalAggregator
from app.analyzers.probability_estimator import estimate_with_breakdown
from app.collectors.polymarket_collector import PolymarketCollector
from app.config import settings
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity

logger = logging.getLogger(__name__)

aggregator = SignalAggregator()
_poly_col = PolymarketCollector()

# Liquidity gate: skip outcomes whose orderbook spread exceeds this. 10¢ = wide.
MAX_BOOK_SPREAD = 0.10

SKIP_QUESTION_KEYWORDS = ("lowest", "daily low", "low temperature", "minimum temp")


def _should_skip_market(question: Optional[str]) -> bool:
    if not question:
        return False
    lo = question.lower()
    return any(kw in lo for kw in SKIP_QUESTION_KEYWORDS)


async def _has_prior_alert(db: AsyncSession, outcome_id: int, side: str) -> bool:
    q = await db.execute(
        select(Opportunity.id).where(
            Opportunity.outcome_id == outcome_id,
            Opportunity.side == side,
            Opportunity.alert_sent == True,
            Opportunity.outcome == None,
        ).limit(1)
    )
    return q.scalar_one_or_none() is not None


async def detect_opportunities(db: AsyncSession) -> List[Opportunity]:
    found: List[Opportunity] = []

    result = await db.execute(
        select(Market).where(Market.resolved == False).order_by(Market.event_date)
    )
    markets: List[Market] = result.scalars().all()

    for market in markets:
        if _should_skip_market(market.question):
            continue

        city_result = await db.execute(select(City).where(City.id == market.city_id))
        city: Optional[City] = city_result.scalar_one_or_none()
        if not city:
            continue

        outcomes_result = await db.execute(
            select(MarketOutcome).where(MarketOutcome.market_id == market.id)
        )
        outcomes: List[MarketOutcome] = outcomes_result.scalars().all()

        is_low_market = False
        city_lat = float(city.nws_lat) if city.nws_lat is not None else None
        city_lon = float(city.nws_lon) if city.nws_lon is not None else None

        best_opp: Optional[Opportunity] = None
        for outcome in outcomes:
            try:
                opp = await _analyze_outcome(
                    db, city, outcome, market, is_low_market, city_lat, city_lon
                )
                if opp is not None:
                    if best_opp is None or opp.edge > best_opp.edge:
                        best_opp = opp
            except Exception as e:
                logger.error(f"Error analyzing outcome {outcome.id}: {e}", exc_info=True)

        if best_opp is not None:
            found.append(best_opp)

    return found


async def _analyze_outcome(
    db: AsyncSession,
    city: City,
    outcome: MarketOutcome,
    market: Market,
    is_low_market: bool,
    city_lat: Optional[float] = None,
    city_lon: Optional[float] = None,
) -> Optional[Opportunity]:
    signals = await aggregator.aggregate(
        db=db,
        city_id=city.id,
        primary_icao=city.primary_icao,
        reference_icao=city.reference_icao,
        outcome=outcome,
        forecast_date=market.event_date,
        is_low_market=is_low_market,
        city_lat=city_lat,
        city_lon=city_lon,
    )

    price_info = signals.get("market_price")
    if not price_info:
        return None

    # ── Liquidity gate ───────────────────────────────────────────────────
    book = None
    if outcome.token_id:
        book = await _poly_col.get_book_summary(outcome.token_id)
    if book is None:
        logger.debug(f"Skipping outcome {outcome.id}: no two-sided orderbook")
        return None
    if book["spread"] > MAX_BOOK_SPREAD:
        logger.debug(
            f"Skipping outcome {outcome.id}: spread {book['spread']:.2f} "
            f"> {MAX_BOOK_SPREAD:.2f}"
        )
        return None

    yes_price_mid = price_info["yes_price"]
    yes_bid = book["bid"]
    yes_ask = book["ask"]
    yes_entry_cost = yes_ask
    no_entry_cost = round(1.0 - yes_bid, 4)

    # ── Probability + breakdown with lead-time-aware σ and gating ───────────────────
    days_ahead = (market.event_date - date.today()).days
    true_prob, breakdown = estimate_with_breakdown(
        signals, outcome.bucket_min, outcome.bucket_max, days_ahead=days_ahead
    )

    if true_prob >= 0.5:
        side = "YES"
        certainty = true_prob
        entry_cost = yes_entry_cost
    else:
        side = "NO"
        certainty = 1.0 - true_prob
        entry_cost = no_entry_cost

    edge = certainty - entry_cost

    min_certainty = max(0.0, min(1.0, settings.min_confidence_for_alert / 100.0))
    if certainty < min_certainty:
        return None
    if edge < settings.min_edge_for_alert:
        return None

    if await _has_prior_alert(db, outcome.id, side):
        logger.debug(f"Dedup: already alerted outcome={outcome.id} side={side}; skipping")
        return None

    signals["_book"] = book
    signals["_entry_cost"] = float(entry_cost)
    signals["_blend"] = breakdown

    opp = Opportunity(
        outcome_id=outcome.id,
        detected_at=datetime.now(timezone.utc),
        side=side,
        market_price=yes_price_mid,
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
