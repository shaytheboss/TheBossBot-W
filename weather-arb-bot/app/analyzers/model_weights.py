"""Per-city model weighting from realized forecast accuracy.

For every settled opportunity we already persist each model's point forecast
(in Opportunity.signals) and, since MarketOutcome.won, the bucket Polymarket
actually settled. This module turns that history into per-city weights:

    weight(model) = 0.5 + hit_rate(model)        # range [0.5, 1.5]

where hit_rate is the Laplace-smoothed fraction of resolved markets on which
the model's predicted daily high landed in the winning bucket. A model with
no track record (or < MIN_SAMPLES) gets the neutral weight 1.0, so behaviour
is identical to the unweighted average until enough evidence accumulates.

Forecasts are de-duplicated per (market, model, detection-day) — markets with
many re-detections would otherwise dominate the tally.

Results are cached in-process per city for CACHE_TTL_SECONDS; the detector
runs every few minutes and weights move on a daily timescale.
"""
import logging
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity
from app.utils.units import resolve_bucket_unit, temp_in_bucket

logger = logging.getLogger(__name__)

MIN_SAMPLES = 5
LOOKBACK_DAYS = 90
MAX_ROWS = 500
CACHE_TTL_SECONDS = 600

# signals forecast key → how to read the model's point forecast.
_WEIGHTED_KEYS: tuple[str, ...] = (
    "gfs_forecast", "ecmwf_forecast", "hrrr_forecast", "nws_forecast",
    "tomorrowio_forecast", "meteosource_forecast", "icon_forecast",
)

_cache: dict[int, tuple[float, dict]] = {}


def _forecast_in_winners(high_f: float, winners: list) -> Optional[bool]:
    """True if high_f (°F) lands in any winning bucket; None when unresolved."""
    if not winners:
        return None
    for unit, bmin, bmax in winners:
        val = (high_f - 32.0) * 5.0 / 9.0 if unit == "C" else high_f
        if temp_in_bucket(bmin, bmax, val):
            return True
    return False


def weights_from_tallies(tallies: dict) -> dict:
    """{key: (correct, total)} → {key: weight}. Pure, unit-testable.

    Laplace smoothing (c+1)/(n+2) keeps small samples near 0.5 (= neutral
    weight 1.0). Keys with fewer than MIN_SAMPLES are omitted (treated as
    neutral by the estimator).
    """
    out: dict = {}
    for key, (correct, total) in tallies.items():
        if total < MIN_SAMPLES:
            continue
        hit_rate = (correct + 1.0) / (total + 2.0)
        out[key] = round(0.5 + hit_rate, 3)
    return out


async def get_city_model_weights(db: AsyncSession, city_id: int) -> dict:
    """signals key → weight for this city (empty dict = all neutral)."""
    now = time.monotonic()
    cached = _cache.get(city_id)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        weights = await _compute_weights(db, city_id)
    except Exception as exc:
        logger.warning("model weights failed for city %s: %s", city_id, exc)
        weights = {}

    _cache[city_id] = (now, weights)
    return weights


async def _compute_weights(db: AsyncSession, city_id: int) -> dict:
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    rows = (await db.execute(
        select(Opportunity.signals, Opportunity.detected_at, Market.id)
        .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
        .join(Market, Market.id == MarketOutcome.market_id)
        .where(
            Market.city_id == city_id,
            Market.resolved == True,
            Opportunity.outcome.in_(["WIN", "LOSS"]),
            Opportunity.detected_at >= since,
        )
        .order_by(Opportunity.detected_at)
        .limit(MAX_ROWS)
    )).all()
    if not rows:
        return {}

    market_ids = {mid for _s, _d, mid in rows}
    won_rows = (await db.execute(
        select(MarketOutcome).where(
            MarketOutcome.market_id.in_(market_ids),
            MarketOutcome.won == True,
        )
    )).scalars().all()
    winners_by_market: dict = {}
    for oc in won_rows:
        winners_by_market.setdefault(oc.market_id, []).append(
            (resolve_bucket_unit(oc), oc.bucket_min, oc.bucket_max)
        )

    # Dedupe: (market, key, detection-day) → forecast value. Rows are ordered
    # by detected_at so later detections overwrite earlier ones for that day.
    dedup: dict = {}
    for signals, detected_at, mid in rows:
        if mid not in winners_by_market or not isinstance(signals, dict):
            continue
        day = detected_at.date() if detected_at else None
        for key in _WEIGHTED_KEYS:
            fc = signals.get(key)
            if not isinstance(fc, dict):
                continue
            v = fc.get("predicted_high_f")
            if v is None:
                continue
            try:
                dedup[(mid, key, day)] = float(v)
            except (TypeError, ValueError):
                continue

    tallies: dict = {}
    for (mid, key, _day), high_f in dedup.items():
        correct = _forecast_in_winners(high_f, winners_by_market.get(mid, []))
        if correct is None:
            continue
        t = tallies.setdefault(key, [0, 0])
        t[1] += 1
        if correct:
            t[0] += 1

    weights = weights_from_tallies({k: tuple(v) for k, v in tallies.items()})
    if weights:
        logger.debug("model weights city=%s: %s", city_id, weights)
    return weights
