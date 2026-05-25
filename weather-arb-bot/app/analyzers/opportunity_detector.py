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

# Standard simulated buy size (per opportunity that clears the buy threshold).
SHARES_PER_BUY = 5


def _alert_and_buy_thresholds(days_ahead: Optional[int]) -> tuple[float, float]:
    """Return (alert_threshold, buy_threshold) in 0-1 range for the given lead.

    near = days_ahead <= 1, far = days_ahead >= 2. If a split setting is
    missing, falls back to ``min_confidence_for_alert / 100``.
    """
    fallback = max(0.0, min(1.0, settings.min_confidence_for_alert / 100.0))
    if days_ahead is not None and days_ahead >= 2:
        alert_t = getattr(settings, "min_confidence_alert_far", None) or fallback
        buy_t = getattr(settings, "min_confidence_buy_far", None) or fallback
    else:
        alert_t = getattr(settings, "min_confidence_alert_near", None) or fallback
        buy_t = getattr(settings, "min_confidence_buy_near", None) or fallback
    # buy >= alert by definition (buy implies alert).
    buy_t = max(buy_t, alert_t)
    return alert_t, buy_t

SKIP_QUESTION_KEYWORDS = ("lowest", "daily low", "low temperature", "minimum temp")


def _should_skip_market(question: Optional[str]) -> bool:
    if not question:
        return False
    lo = question.lower()
    return any(kw in lo for kw in SKIP_QUESTION_KEYWORDS)


async def _has_opportunity_today(db: AsyncSession, outcome_id: int, side: str) -> bool:
    """True if we already created an opportunity for this outcome+side today.

    Using a per-calendar-day window (not a permanent block) lets multi-day markets
    be re-evaluated each morning as the forecast updates, while still preventing
    duplicate alerts within the same day.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    q = await db.execute(
        select(Opportunity.id).where(
            Opportunity.outcome_id == outcome_id,
            Opportunity.side == side,
            Opportunity.detected_at >= today_start,
        ).limit(1)
    )
    return q.scalar_one_or_none() is not None


async def _get_prior_opportunity(
    db: AsyncSession, outcome_id: int, side: str
) -> Optional[Opportunity]:
    """Return the most recent prior Opportunity for this (outcome_id, side), if any.

    Looks across all time (not just today) so UPDATE messages can reference
    the previous alert even for markets detected on a prior day.
    """
    result = await db.execute(
        select(Opportunity)
        .where(Opportunity.outcome_id == outcome_id)
        .where(Opportunity.side == side)
        .order_by(Opportunity.detected_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def detect_opportunities(db: AsyncSession) -> List[Opportunity]:
    found: List[Opportunity] = []

    result = await db.execute(
        select(Market).where(Market.resolved == False).order_by(Market.event_date)
    )
    markets: List[Market] = result.scalars().all()

    for market in markets:
        if _should_skip_market(market.question):
            continue

        days_ahead = (market.event_date - date.today()).days
        if days_ahead < 0:
            # Already past; resolution job will mark it resolved.
            continue
        if days_ahead > settings.max_days_ahead_for_alert:
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
                    db, city, outcome, market, is_low_market, city_lat, city_lon, days_ahead
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
    days_ahead: int = 0,
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

    # ── Liquidity gate ──────────────────────────────────────────────────
    book = None
    if outcome.token_id:
        book = await _poly_col.get_book_summary(outcome.token_id)
    if book is None:
        logger.debug(f"Skipping outcome {outcome.id}: no two-sided orderbook")
        return None
    if book["bid"] == 0 and book["ask"] == 0:
        logger.debug(f"Skipping outcome {outcome.id}: market closed (bid=ask=0)")
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

    # ── Probability + breakdown ──────────────────────────────────────────────────
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

    alert_thresh, buy_thresh = _alert_and_buy_thresholds(days_ahead)
    if certainty < alert_thresh:
        return None
    if edge < settings.min_edge_for_alert:
        return None
    create_virtual_buy = certainty >= buy_thresh

    # ── Dedup: one opportunity per outcome+side per calendar day ──────────────────
    # Multi-day markets are re-evaluated each morning so forecasts can shift
    # the recommendation. Same-day markets alert at most once.
    if await _has_opportunity_today(db, outcome.id, side):
        logger.debug(f"Dedup: already have opportunity for outcome={outcome.id} side={side} today")
        return None

    # Look up any prior opportunity for this (outcome, side) so the formatter
    # can generate an UPDATE message. We do this after dedup so we only reach
    # here if there's no existing opportunity *today* — any match here is from
    # a prior day.
    prior_opp = await _get_prior_opportunity(db, outcome.id, side)
    # If prior_opp exists and was created today it would have been caught by
    # _has_opportunity_today above, so any match here is genuinely from a prior period.

    signals["_book"] = book
    signals["_entry_cost"] = float(entry_cost)
    signals["_blend"] = breakdown
    signals["_prior_opportunity_id"] = prior_opp.id if prior_opp else None
    # Surface threshold metadata for the formatter (so the alert can explain
    # whether a virtual buy was made or not, and against which threshold).
    signals["_alert_threshold"] = float(alert_thresh)
    signals["_buy_threshold"] = float(buy_thresh)
    signals["_create_virtual_buy"] = bool(create_virtual_buy)

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
    if create_virtual_buy:
        # Per-share entry price is the actual ASK we'd pay for the bet side:
        # YES → book["ask"]; NO → 1 - book["bid"] (i.e. the NO ask).
        # entry_cost above is already computed with this convention.
        opp.virtual_shares = SHARES_PER_BUY
        opp.virtual_entry_price = float(entry_cost)
        opp.virtual_cost = float(SHARES_PER_BUY) * float(entry_cost)
        opp.virtual_status = "open"
    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return opp
