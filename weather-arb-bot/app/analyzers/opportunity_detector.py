import logging
from datetime import date, datetime, timedelta, timezone
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
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)

aggregator = SignalAggregator()
_poly_col = PolymarketCollector()

MAX_BOOK_SPREAD = 0.10

SHARES_PER_BUY = 5


def _alert_and_buy_thresholds(days_ahead: Optional[int]) -> tuple[float, float]:
    fallback = max(0.0, min(1.0, settings.min_confidence_for_alert / 100.0))
    if days_ahead is not None and days_ahead >= 2:
        alert_t = getattr(settings, "min_confidence_alert_far", None) or fallback
        buy_t = getattr(settings, "min_confidence_buy_far", None) or fallback
    else:
        alert_t = getattr(settings, "min_confidence_alert_near", None) or fallback
        buy_t = getattr(settings, "min_confidence_buy_near", None) or fallback
    buy_t = max(buy_t, alert_t)
    return alert_t, buy_t

SKIP_QUESTION_KEYWORDS = ("lowest", "daily low", "low temperature", "minimum temp")


def _should_skip_market(question: Optional[str]) -> bool:
    if not question:
        return False
    lo = question.lower()
    return any(kw in lo for kw in SKIP_QUESTION_KEYWORDS)


async def _has_opportunity_today(db: AsyncSession, outcome_id: int, side: str) -> bool:
    """True if any opportunity (either side) was already created for this outcome today.

    We block both sides — not just the same side — to prevent the detector
    from flipping YES→NO (or NO→YES) on the same outcome within one day.
    The `side` parameter is kept in the signature for call-site compatibility
    but is intentionally not used in the query.
    """
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    q = await db.execute(
        select(Opportunity.id).where(
            Opportunity.outcome_id == outcome_id,
            Opportunity.detected_at >= today_start,
        ).limit(1)
    )
    return q.scalar_one_or_none() is not None


async def _market_alert_cooldown_active(
    db: AsyncSession, market_id: int, minutes: int
) -> bool:
    """True if any alert was sent for this market within the cooldown window.

    Without this, the analyzer can flip its recommendation between outcomes
    of the same market within minutes (e.g. alerting '84-85°F NO @ 86%' at
    10:00 and '82-83°F YES @ 75%' at 10:05) because new price/forecast data
    shifts which outcome wins the best-edge selection. Per-(outcome, side)
    dedup doesn't catch this; only a market-level cooldown does.
    """
    if minutes <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    q = await db.execute(
        select(Opportunity.id)
        .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
        .where(
            MarketOutcome.market_id == market_id,
            Opportunity.alert_sent == True,
            Opportunity.detected_at >= cutoff,
        )
        .limit(1)
    )
    return q.scalar_one_or_none() is not None


async def _get_prior_opportunity(
    db: AsyncSession, outcome_id: int, side: str
) -> Optional[Opportunity]:
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

    cooldown_minutes = int(getattr(settings, "alert_dedup_minutes", 0) or 0)

    for market in markets:
        if _should_skip_market(market.question):
            continue

        days_ahead = (market.event_date - date.today()).days
        if days_ahead < 0:
            continue
        if days_ahead > settings.max_days_ahead_for_alert:
            continue

        # Market-level alert cooldown: if any alert was sent for any outcome
        # in this market within the last `alert_dedup_minutes` minutes, skip
        # the entire market to prevent flipping recommendations.
        if cooldown_minutes > 0 and await _market_alert_cooldown_active(
            db, market.id, cooldown_minutes
        ):
            logger.debug(
                f"Market {market.id} ({market.external_id}) in alert cooldown "
                f"({cooldown_minutes}min); skipping"
            )
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

    # Defensive: when the bucket_unit column on the DB hasn't been backfilled
    # by migration 005 yet, fall back to detecting Celsius from the label.
    # Without this, native C integers (e.g. 29 for "29°C or higher") get fed
    # into the estimator as Fahrenheit → P(>=29°F) ≈ 100% for any plausible
    # forecast, producing spurious 1-6¢ virtual buys at "97% confidence".
    bucket_unit = resolve_bucket_unit(outcome)
    true_prob, breakdown = estimate_with_breakdown(
        signals,
        outcome.bucket_min,
        outcome.bucket_max,
        days_ahead=days_ahead,
        bucket_unit=bucket_unit,
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

    # Reject suspiciously large edge: when our estimate diverges from the
    # market by more than max_edge_for_alert, the market is almost certainly
    # pricing in information we lack (breaking news, ICAO mismatch, tropical
    # regime, etc.). Treat extreme edge as a model-quality failure signal.
    max_edge = float(getattr(settings, "max_edge_for_alert", 1.0))
    if edge > max_edge:
        logger.debug(
            f"Skipping outcome {outcome.id} ({outcome.bucket_label}): "
            f"edge {edge:.2f} > max_edge {max_edge:.2f} — market strongly disagrees"
        )
        return None

    create_virtual_buy = certainty >= buy_thresh

    if await _has_opportunity_today(db, outcome.id, side):
        logger.debug(f"Dedup: already have opportunity for outcome={outcome.id} today")
        return None

    prior_opp = await _get_prior_opportunity(db, outcome.id, side)

    signals["_book"] = book
    signals["_entry_cost"] = float(entry_cost)
    signals["_blend"] = breakdown
    signals["_prior_opportunity_id"] = prior_opp.id if prior_opp else None
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
        opp.virtual_shares = SHARES_PER_BUY
        opp.virtual_entry_price = float(entry_cost)
        opp.virtual_cost = float(SHARES_PER_BUY) * float(entry_cost)
        opp.virtual_status = "open"
    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return opp
