"""Beta opportunity detector — runs the beta estimator in full isolation.

Mirrors the structure of opportunity_detector.py but:
  - Uses beta_estimator.beta_estimate_with_breakdown() instead of alpha
  - Loads per-city model_skill data from DB and passes it to the estimator
  - Writes Opportunity rows tagged with estimator="beta"
  - Maintains separate dedup state (never touches alpha's module-level dicts)
  - Wrapped entirely in its own try/except at every level so it CANNOT affect alpha

The alpha path (opportunity_detector.py) is NOT imported here.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.analyzers.signal_aggregator import SignalAggregator
from app.analyzers.beta_estimator import (
    beta_estimate_with_breakdown,
    beta_blend_with_market,
    _clip as _beta_clip,
)
from app.collectors.polymarket_collector import PolymarketCollector
from app.config import settings
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.model_skill import ModelSkill
from app.models.opportunity import Opportunity
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)

_beta_aggregator = SignalAggregator()
_beta_poly_col = PolymarketCollector()

BETA_SHARES_PER_BUY = 5
BETA_MAX_BOOK_SPREAD = 0.10

# Separate dedup state — alpha's _open_position_last_sent is never read here
_beta_dedup_date: Optional[date] = None
_beta_open_pos_last_sent: dict[tuple, float] = {}

SKIP_KEYWORDS = ("lowest", "daily low", "low temperature", "minimum temp")


def _reset_beta_dedup_if_new_day() -> None:
    global _beta_dedup_date
    today = date.today()
    if _beta_dedup_date != today:
        _beta_dedup_date = today
        _beta_open_pos_last_sent.clear()


def _beta_city_is_suspended(city: City) -> bool:
    if not getattr(city, "suspended_until", None):
        return False
    return city.suspended_until > datetime.now(timezone.utc)


def _beta_alert_and_buy_thresholds(days_ahead: Optional[int]) -> tuple[float, float]:
    fallback = max(0.0, min(1.0, settings.min_confidence_for_alert / 100.0))

    def _or_fallback(name: str) -> float:
        v = getattr(settings, name, None)
        return fallback if v is None else float(v)

    if days_ahead is not None and days_ahead >= 2:
        alert_t = _or_fallback("min_confidence_alert_far")
        buy_t = _or_fallback("min_confidence_buy_far")
    else:
        alert_t = _or_fallback("min_confidence_alert_near")
        buy_t = _or_fallback("min_confidence_buy_near")
    buy_t = max(buy_t, alert_t)
    return alert_t, buy_t


async def _beta_has_open_position(
    db: AsyncSession, outcome_id: int, side: str
) -> bool:
    q = await db.execute(
        select(Opportunity.id).where(
            Opportunity.outcome_id == outcome_id,
            Opportunity.side == side,
            Opportunity.virtual_status == "open",
            Opportunity.estimator == "beta",
        ).limit(1)
    )
    return q.scalar_one_or_none() is not None


async def _beta_has_opportunity_today(
    db: AsyncSession, outcome_id: int, side: str
) -> bool:
    today_start = datetime.combine(
        date.today(), datetime.min.time()
    ).replace(tzinfo=timezone.utc)
    q = await db.execute(
        select(Opportunity.id).where(
            Opportunity.outcome_id == outcome_id,
            Opportunity.side == side,
            Opportunity.detected_at >= today_start,
            Opportunity.estimator == "beta",
        ).limit(1)
    )
    return q.scalar_one_or_none() is not None


@dataclass(frozen=True)
class _SkillSnapshot:
    """Lightweight immutable substitute for a ModelSkill ORM row.

    Used when aggregating across lead-times so that beta's per-city
    corrections (bias, sigma, weight) activate even when any single
    days_ahead slot has fewer than MIN_SAMPLES resolved markets.
    The estimator accesses the same field names as ModelSkill via
    duck typing — no changes needed there.
    """
    samples: int
    hits: int
    hit_rate: Optional[float]
    mae_f: Optional[float]
    bias_f: Optional[float]
    weight: float = 1.0


async def _load_city_skill(
    db: AsyncSession, city_id: int, days_ahead: int
) -> dict:
    """Load ModelSkill rows for (city_id, days_ahead). Returns {source: row}.

    Primary path: exact days_ahead match with >= MIN_SAMPLES → use as-is.

    Fallback (the key fix): when a source has fewer than MIN_SAMPLES at the
    requested lead-time, aggregate across ALL available lead-times for that
    (city, source) pair into a _SkillSnapshot.  This unlocks beta's per-city
    bias/sigma/weight corrections after ~MIN_SAMPLES total resolved markets
    instead of requiring MIN_SAMPLES *per lead-time bucket* — which was
    effectively starving beta of calibration data for the first months of use.

    When even the combined total is < MIN_SAMPLES, the thin exact row is
    returned (if it exists) so the breakdown dict can log it; the estimator
    naturally treats it as neutral via the MIN_SAMPLES guard in
    _beta_source_weight.
    """
    from app.analyzers.model_skill import MIN_SAMPLES, skill_weight

    # Load ALL lead-time rows for this city in a single query.
    all_rows = (await db.execute(
        select(ModelSkill).where(ModelSkill.city_id == city_id)
    )).scalars().all()

    exact: dict[str, ModelSkill] = {}
    by_source: dict[str, list] = {}
    for r in all_rows:
        by_source.setdefault(r.source, []).append(r)
        if r.days_ahead == days_ahead:
            exact[r.source] = r

    def _wgt_avg(pairs: list) -> Optional[float]:
        total_n = sum(n for _, n in pairs)
        return sum(v * n for v, n in pairs) / total_n if total_n > 0 else None

    result: dict = {}
    for source, rows in by_source.items():
        exact_row = exact.get(source)
        if exact_row is not None and (exact_row.samples or 0) >= MIN_SAMPLES:
            # Exact lead-time has enough data — use it directly.
            result[source] = exact_row
            continue

        # Aggregate all lead-times for this source.
        total_samples = sum(r.samples or 0 for r in rows)
        total_hits = sum(r.hits or 0 for r in rows)

        if total_samples < MIN_SAMPLES:
            # Still too thin combined — keep the thin exact row for breakdown
            # logging but the estimator will treat it as neutral.
            if exact_row is not None:
                result[source] = exact_row
            continue

        mae_pairs = [(r.mae_f, r.samples or 0) for r in rows if r.mae_f is not None]
        bias_pairs = [(r.bias_f, r.samples or 0) for r in rows if r.bias_f is not None]
        agg_mae = _wgt_avg(mae_pairs)
        agg_bias = _wgt_avg(bias_pairs)

        result[source] = _SkillSnapshot(
            samples=total_samples,
            hits=total_hits,
            hit_rate=round(total_hits / total_samples, 4),
            mae_f=round(agg_mae, 2) if agg_mae is not None else None,
            bias_f=round(agg_bias, 2) if agg_bias is not None else None,
            weight=skill_weight(total_hits, total_samples),
        )

    return result


def _normalization_scale(raw_probs: list, n_market_outcomes: int) -> Optional[float]:
    if len(raw_probs) != n_market_outcomes:
        return None
    total = sum(raw_probs)
    return None if total <= 0.1 else 1.0 / total


async def _beta_collect_outcome_data(
    db: AsyncSession,
    city: City,
    outcome: MarketOutcome,
    market: Market,
    city_lat: Optional[float],
    city_lon: Optional[float],
    days_ahead: int,
    city_skill: dict,
) -> Optional[dict]:
    signals = await _beta_aggregator.aggregate(
        db=db,
        city_id=city.id,
        primary_icao=city.primary_icao,
        reference_icao=city.reference_icao,
        outcome=outcome,
        forecast_date=market.event_date,
        is_low_market=False,
        city_lat=city_lat,
        city_lon=city_lon,
        city_tz=city.timezone,
        onshore_wind_dir=getattr(city, "onshore_wind_dir", None),
    )

    if not signals.get("market_price"):
        return None

    book = None
    if outcome.token_id:
        try:
            book = await _beta_poly_col.get_book_summary(outcome.token_id)
        except Exception as e:
            logger.debug(f"[beta] Book fetch failed for outcome {outcome.id}: {e}")

    bucket_unit = resolve_bucket_unit(outcome)
    raw_prob, breakdown = beta_estimate_with_breakdown(
        signals,
        outcome.bucket_min,
        outcome.bucket_max,
        days_ahead=days_ahead,
        bucket_unit=bucket_unit,
        city_skill=city_skill,
    )

    if not breakdown.get("has_forecast_data"):
        return None

    return {
        "outcome": outcome,
        "signals": signals,
        "raw_prob": raw_prob,
        "normalized_prob": raw_prob,
        "breakdown": breakdown,
        "book": book,
        "bucket_unit": bucket_unit,
    }


async def _beta_evaluate_opportunity(
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
    if book is None:
        return None
    if book["bid"] == 0 and book["ask"] == 0:
        return None
    if book["spread"] > BETA_MAX_BOOK_SPREAD:
        return None

    price_info = signals.get("market_price") or {}
    yes_price_mid = price_info.get("yes_price", 0.5)
    yes_entry_cost = book["ask"]
    no_entry_cost = round(1.0 - book["bid"], 4)

    if true_prob >= 0.5:
        side = "YES"
        certainty = true_prob
        entry_cost = yes_entry_cost
    else:
        side = "NO"
        certainty = 1.0 - true_prob
        entry_cost = no_entry_cost

    edge = certainty - entry_cost
    alert_thresh, buy_thresh = _beta_alert_and_buy_thresholds(days_ahead)

    if certainty < alert_thresh:
        return None
    if edge < settings.min_edge_for_alert:
        return None
    max_edge = float(getattr(settings, "max_edge_for_alert", 1.0))
    if edge > max_edge:
        return None

    is_blacklisted = bool(getattr(city, "blacklisted", False))
    is_suspended = _beta_city_is_suspended(city)
    # Beta uses its OWN virtual-buy threshold, not alpha's buy_thresh (0.90).
    # The market-blend step (60/40 pull toward market price) structurally caps
    # beta's blended certainty around ~88%, so alpha's gate became unreachable
    # and beta collapsed from ~125 virtual buys/week to ~1/week — with no
    # positions there is no calibration data. Beta buys are virtual-only, so
    # the lower gate costs nothing and keeps the learning loop alive.
    beta_buy_thresh = max(
        float(getattr(settings, "min_confidence_beta_buy", 0.85)),
        alert_thresh,
    )
    create_virtual_buy = (certainty >= beta_buy_thresh) and not is_blacklisted and not is_suspended

    # Beta-scoped dedup: only looks at beta rows
    if (
        await _beta_has_open_position(db, outcome.id, side)
        or await _beta_has_opportunity_today(db, outcome.id, side)
    ):
        logger.debug(f"[beta] Dedup: beta opportunity already exists for outcome={outcome.id} {side}")
        return None

    signals["_beta_breakdown"] = breakdown
    signals["_entry_cost"] = float(entry_cost)
    signals["_create_virtual_buy"] = bool(create_virtual_buy)
    signals["_city_blacklisted"] = is_blacklisted
    signals["_city_suspended"] = is_suspended
    signals["_shares_per_buy"] = BETA_SHARES_PER_BUY
    signals["_estimator"] = "beta"
    signals["_beta_blocked_sources"] = breakdown.get("blocked_sources", [])
    signals["_beta_is_variance_city"] = bool(breakdown.get("is_variance_city"))

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
        estimator="beta",
    )
    if create_virtual_buy:
        opp.virtual_shares = BETA_SHARES_PER_BUY
        opp.virtual_entry_price = float(entry_cost)
        opp.virtual_cost = float(BETA_SHARES_PER_BUY) * float(entry_cost)
        opp.virtual_status = "open"

    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return opp


async def detect_beta_opportunities(db: AsyncSession) -> List[Opportunity]:
    """Run the beta detection cycle. Completely isolated from the alpha path.

    Reads the same markets and signals as alpha but applies the calibrated
    beta estimator. Returns newly created beta Opportunity rows.
    Never raises — all exceptions are caught internally.
    """
    found: List[Opportunity] = []
    _reset_beta_dedup_if_new_day()

    try:
        result = await db.execute(
            select(Market).where(Market.resolved == False).order_by(Market.event_date)
        )
        markets: List[Market] = result.scalars().all()
    except Exception as e:
        logger.error(f"[beta] Failed to load markets: {e}", exc_info=True)
        return found

    for market in markets:
        try:
            question = market.question or ""
            if any(kw in question.lower() for kw in SKIP_KEYWORDS):
                continue

            days_ahead = (market.event_date - date.today()).days
            if days_ahead < 0 or days_ahead > settings.max_days_ahead_for_alert:
                continue

            city_result = await db.execute(select(City).where(City.id == market.city_id))
            city: Optional[City] = city_result.scalar_one_or_none()
            if not city:
                continue

            outcomes_result = await db.execute(
                select(MarketOutcome).where(MarketOutcome.market_id == market.id)
            )
            outcomes: List[MarketOutcome] = outcomes_result.scalars().all()

            city_lat = float(city.nws_lat) if city.nws_lat is not None else None
            city_lon = float(city.nws_lon) if city.nws_lon is not None else None

            try:
                city_skill = await _load_city_skill(db, city.id, days_ahead)
            except Exception as e:
                logger.warning(f"[beta] Skill load failed for {city.name}: {e}")
                city_skill = {}

            outcome_data = []
            for outcome in outcomes:
                try:
                    data = await _beta_collect_outcome_data(
                        db, city, outcome, market,
                        city_lat, city_lon, days_ahead, city_skill,
                    )
                    if data is not None:
                        outcome_data.append(data)
                except Exception as e:
                    logger.error(
                        f"[beta] Error collecting data for outcome {outcome.id}: {e}",
                        exc_info=True,
                    )

            if not outcome_data:
                continue

            scale = _normalization_scale(
                [d["raw_prob"] for d in outcome_data], len(outcomes)
            )
            for d in outcome_data:
                if scale is not None:
                    d["normalized_prob"] = _beta_clip(d["raw_prob"] * scale)
                    d["breakdown"]["normalization_scale"] = round(scale, 4)
                else:
                    d["breakdown"]["normalization_scale"] = None
                # Temper toward the market price (beta's edge is anti-predictive;
                # the market tracks realized outcomes far better). Applied after
                # normalization so it operates on beta's final estimate.
                market_prob = (d["signals"].get("market_price") or {}).get("yes_price")
                blended, blend_info = beta_blend_with_market(
                    d["normalized_prob"], market_prob
                )
                d["normalized_prob"] = _beta_clip(blended)
                d["breakdown"]["market_blend"] = blend_info
                d["breakdown"]["normalized_final"] = round(float(d["normalized_prob"]), 4)

            for d in outcome_data:
                try:
                    opp = await _beta_evaluate_opportunity(
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
                        found.append(opp)
                except Exception as e:
                    logger.error(
                        f"[beta] Error evaluating outcome {d['outcome'].id}: {e}",
                        exc_info=True,
                    )

        except Exception as e:
            logger.error(
                f"[beta] Error processing market {market.id}: {e}", exc_info=True
            )

    return found
