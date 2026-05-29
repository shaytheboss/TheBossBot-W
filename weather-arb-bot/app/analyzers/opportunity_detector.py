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
    """True if any opportunity was already created for this outcome today."""
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


def _compute_why_now(
    signals: dict,
    breakdown: dict,
    prior_opp: Optional[Opportunity],
) -> Optional[str]:
    """Short string describing what changed since the last alert on this outcome."""
    if prior_opp is None:
        return None
    prior_signals = prior_opp.signals or {}
    prior_blend = prior_signals.get("_blend") or {}

    curr_det = {d["source"]: d["value_f"] for d in (breakdown.get("deterministic") or [])}
    prior_det = {d["source"]: d["value_f"] for d in (prior_blend.get("deterministic") or [])}

    changes = []
    for src, curr_val in curr_det.items():
        prior_val = prior_det.get(src)
        if prior_val is None:
            continue
        delta = curr_val - prior_val
        if abs(delta) >= 1.0:
            short = src.split(" ")[0].split("(")[0].strip()
            sign = "↑" if delta > 0 else "↓"
            changes.append((abs(delta), f"{short} {sign}{abs(round(delta, 1))}°F"))

    changes.sort(reverse=True)

    prior_price = float(prior_opp.market_price) if prior_opp.market_price else 0.0
    curr_price = (signals.get("market_price") or {}).get("yes_price") or 0.0
    price_delta = curr_price - prior_price

    parts = [c[1] for c in changes[:2]]
    if abs(price_delta) >= 0.03:
        sign = "↑" if price_delta > 0 else "↓"
        parts.append(f"mkt {sign}{round(abs(price_delta) * 100)}¢")

    return " | ".join(parts) if parts else None


async def _collect_outcome_data(
    db: AsyncSession,
    city: City,
    outcome: MarketOutcome,
    market: Market,
    is_low_market: bool,
    city_lat: Optional[float],
    city_lon: Optional[float],
    days_ahead: int,
) -> Optional[dict]:
    """Aggregate signals and compute raw probability for one outcome.

    Returns None if market price data is missing (outcome not tracked).
    book may be None when not tradeable -- still included for normalization.
    """
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
        try:
            book = await _poly_col.get_book_summary(outcome.token_id)
        except Exception as e:
            logger.debug(f"Book fetch failed for outcome {outcome.id}: {e}")

    bucket_unit = resolve_bucket_unit(outcome)
    raw_prob, breakdown = estimate_with_breakdown(
        signals,
        outcome.bucket_min,
        outcome.bucket_max,
        days_ahead=days_ahead,
        bucket_unit=bucket_unit,
    )

    return {
        "outcome": outcome,
        "signals": signals,
        "raw_prob": raw_prob,
        "normalized_prob": raw_prob,
        "breakdown": breakdown,
        "book": book,
        "bucket_unit": bucket_unit,
    }


async def _evaluate_opportunity(
    db: AsyncSession,
    city: City,
    outcome: MarketOutcome,
    market: Market,
    true_prob: float,
    breakdown: dict,
    signals: dict,
    book: Optional[dict],
    days_ahead: int,
) -> Optional[Opportunity]:
    """Given a (possibly normalized) probability, decide whether to create an Opportunity."""
    if book is None:
        logger.debug(f"Skipping outcome {outcome.id}: no two-sided orderbook")
        return None
    if book["bid"] == 0 and book["ask"] == 0:
        logger.debug(f"Skipping outcome {outcome.id}: market closed (bid=ask=0)")
        return None
    if book["spread"] > MAX_BOOK_SPREAD:
        logger.debug(
            f"Skipping outcome {outcome.id}: spread {book['spread']:.2f} > {MAX_BOOK_SPREAD:.2f}"
        )
        return None

    price_info = signals.get("market_price") or {}
    yes_price_mid = price_info.get("yes_price", 0.5)
    yes_bid = book["bid"]
    yes_entry_cost = book["ask"]
    no_entry_cost = round(1.0 - yes_bid, 4)

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

    max_edge = float(getattr(settings, "max_edge_for_alert", 1.0))
    if edge > max_edge:
        logger.debug(
            f"Skipping outcome {outcome.id} ({outcome.bucket_label}): "
            f"edge {edge:.2f} > max_edge {max_edge:.2f} -- market strongly disagrees"
        )
        return None

    create_virtual_buy = certainty >= buy_thresh

    if await _has_opportunity_today(db, outcome.id, side):
        logger.debug(f"Dedup: already have opportunity for outcome={outcome.id} today")
        return None

    prior_opp = await _get_prior_opportunity(db, outcome.id, side)
    why_now = _compute_why_now(signals, breakdown, prior_opp)

    signals["_book"] = book
    signals["_entry_cost"] = float(entry_cost)
    signals["_blend"] = breakdown
    signals["_prior_opportunity_id"] = prior_opp.id if prior_opp else None
    signals["_alert_threshold"] = float(alert_thresh)
    signals["_buy_threshold"] = float(buy_thresh)
    signals["_create_virtual_buy"] = bool(create_virtual_buy)
    signals["_why_now"] = why_now

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

        # Phase 1: collect raw probabilities for all outcomes in this market
        outcome_data = []
        for outcome in outcomes:
            try:
                data = await _collect_outcome_data(
                    db, city, outcome, market, is_low_market, city_lat, city_lon, days_ahead
                )
                if data is not None:
                    outcome_data.append(data)
            except Exception as e:
                logger.error(f"Error collecting data for outcome {outcome.id}: {e}", exc_info=True)

        if not outcome_data:
            continue

        # Phase 2: C -- normalize so all bucket probabilities sum to 1
        total_raw = sum(d["raw_prob"] for d in outcome_data)
        if total_raw > 0.1:
            scale = 1.0 / total_raw
            for d in outcome_data:
                d["normalized_prob"] = d["raw_prob"] * scale
                d["breakdown"]["normalization_scale"] = round(scale, 4)

        # Phase 3: evaluate each outcome, pick best edge
        best_opp: Optional[Opportunity] = None
        for d in outcome_data:
            try:
                opp = await _evaluate_opportunity(
                    db=db,
                    city=city,
                    outcome=d["outcome"],
                    market=market,
                    true_prob=d["normalized_prob"],
                    breakdown=d["breakdown"],
                    signals=d["signals"],
                    book=d["book"],
                    days_ahead=days_ahead,
                )
                if opp is not None:
                    if best_opp is None or opp.edge > best_opp.edge:
                        best_opp = opp
            except Exception as e:
                logger.error(f"Error evaluating outcome {d['outcome'].id}: {e}", exc_info=True)

        if best_opp is not None:
            found.append(best_opp)

    return found
