"""The model's view of every bucket in a market, computed the production way.

Reuse, not reimplementation. Everything here calls the production functions:
SignalAggregator.aggregate, estimate_with_breakdown, normalization_scale, the
probability clip, and the Celsius-bucket conversion. A study that recomputed
the estimate its own way would be measuring a different model from the one
that trades.

The one deliberate difference is efficiency. aggregate() runs about eighteen
queries, and only the last few lines depend on the specific bucket — every
forecast, METAR reading, bias and skill weight is identical across a market's
buckets. So it runs ONCE per market, and each bucket gets a copy with just its
own fields replaced. For an eleven-bucket market that is ~29 queries instead of
~198. A flow test compares the result against calling aggregate() per bucket,
so if someone later adds another bucket-dependent field to aggregate(), the
test fails rather than the study silently drifting from production.

What this module deliberately does NOT call:

  _collect_outcome_data     fetches the order book over HTTP
  _persist_collector_misses writes to a production table
  _evaluate_intraday_outcome reads the book, writes positions, and registers
                            cluster warm-ups — the study takes only the
                            estimate from it, through the helpers it shares

The intraday probability. For a market on the city's local today, inside the
intraday hours, each bucket also gets the intraday model's P(YES): the one
that knows the running max. The daily model cannot see the thermometer, so in
the last hours of the day only this one is a fair match for the market.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pytz

from app.analyzers.opportunity_detector import normalization_scale
from app.analyzers.probability_estimator import (
    _DET_SOURCES,
    _clip as _prob_clip,
    estimate_with_breakdown,
)
from app.analyzers.signal_aggregator import SignalAggregator, _c_bucket_to_f_int_range
from app.intraday.detector import (
    _minutes_since_running_max,
    _params_from_settings,
    apply_cluster_boost,
    blended_forecast_high,
    cluster_boost_for,
    forecast_spread,
    observed_readings,
)
from app.intraday.estimator import estimate_intraday, local_decimal_hour
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)

#: Keys of aggregate()'s output that depend on the individual bucket. Every
#: other key is shared by all of a market's buckets. Pinned by a flow test.
OUTCOME_SPECIFIC_KEYS = (
    "market_price", "_bucket_unit", "_bucket_native_min", "_bucket_native_max",
    "_bucket_min", "_bucket_max",
)


@dataclass
class BucketEstimate:
    outcome_id: int
    raw_p: float
    model_p: float
    normalized: bool
    market_p: Optional[float]
    n_sources: Optional[int]
    forecast_high_f: Optional[float]
    sigma: Optional[float]
    forecast_age_min: Optional[int]
    # The intraday model's P(YES); None outside its hours or without METAR.
    intraday_p: Optional[float] = None


def intraday_hour(city, market, now: datetime, params) -> Optional[float]:
    """The city's local decimal hour if the intraday model would run on this
    market now — the market is for the city's local today and the hour is past
    the intraday start — else None. The same two gates detect_intraday applies."""
    try:
        tz = pytz.timezone(city.timezone) if city.timezone else pytz.utc
    except Exception:
        tz = pytz.utc
    if market.event_date != now.astimezone(tz).date():
        return None
    loc_hour = local_decimal_hour(now, tz)
    return loc_hour if loc_hour >= params.start_hour else None


async def intraday_probabilities(db, city, market, outcomes: list, base: dict,
                                 now: datetime) -> dict[int, float]:
    """{outcome_id: intraday P(YES)} computed as the detector computes it, or
    {} when the intraday model would not run. Reads only; writes nothing."""
    params = _params_from_settings()
    loc_hour = intraday_hour(city, market, now, params)
    if loc_hour is None:
        return {}
    readings = observed_readings(base, now)
    if readings is None:
        return {}
    try:
        tz = pytz.timezone(city.timezone) if city.timezone else pytz.utc
    except Exception:
        tz = pytz.utc
    minutes_since_max = await _minutes_since_running_max(db, city.primary_icao, tz, now)

    signals = dict(base)      # the boost replaces station_bias; base is untouched
    boost, note = cluster_boost_for(city.name)
    if boost > 0:
        apply_cluster_boost(signals, boost, note)
    forecast_high = blended_forecast_high(signals)
    spread_f = forecast_spread(signals)

    out = {}
    for outcome in outcomes:
        p, _ = estimate_intraday(
            running_max_f=readings["running_max"],
            current_temp_f=readings["current_temp"],
            minutes_since_max=minutes_since_max,
            forecast_high_f=forecast_high,
            local_hour=loc_hour,
            bucket_min=outcome.bucket_min,
            bucket_max=outcome.bucket_max,
            bucket_unit=resolve_bucket_unit(outcome),
            params=params,
            metar_max_f=readings["metar_max"],
            forecast_spread_f=spread_f,
            wu_confirmed=readings["wu_confirmed"],
        )
        out[outcome.id] = float(p)
    return out


def signals_for_outcome(base: dict, outcome, market_price: Optional[dict]) -> dict:
    """`base` with only this bucket's fields replaced — exactly the block
    aggregate() computes last. Shallow copy: the estimator only reads signals
    (checked), so sharing the nested per-market values is safe."""
    s = dict(base)
    unit = resolve_bucket_unit(outcome)
    s["_bucket_unit"] = unit
    s["_bucket_native_min"] = outcome.bucket_min
    s["_bucket_native_max"] = outcome.bucket_max
    if unit == "C":
        s["_bucket_min"], s["_bucket_max"] = _c_bucket_to_f_int_range(
            outcome.bucket_min, outcome.bucket_max
        )
    else:
        s["_bucket_min"], s["_bucket_max"] = outcome.bucket_min, outcome.bucket_max
    s["market_price"] = market_price
    return s


def newest_forecast_age_min(signals: dict, now: datetime) -> Optional[int]:
    """Minutes since the most recent deterministic forecast landed.

    This is the field that tests the "just after a model run" hypothesis: if
    the model has an edge, it should be largest when this number is small and
    the market has not caught up yet.
    """
    newest: Optional[datetime] = None
    for key, _label, _global in _DET_SOURCES:
        ts = (signals.get(key) or {}).get("retrieved_at")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=now.tzinfo)
        if newest is None or t > newest:
            newest = t
    if newest is None:
        return None
    return max(0, int((now - newest).total_seconds() // 60))


async def estimate_market(
    db,
    city,
    market,
    outcomes: list,
    now: datetime,
    aggregator: Optional[SignalAggregator] = None,
    today: Optional[date] = None,
) -> list[BucketEstimate]:
    """Every bucket's estimate for one market, or [] if there is no forecast.

    Mirrors the detector: an outcome with no forecast data is not estimated
    (the estimate would be the flat fallback), and normalisation across the
    market is applied only when production would apply it — every bucket
    priced. Unpriced buckets keep their raw probability, flagged as such.
    """
    if not outcomes:
        return []
    agg = aggregator or SignalAggregator()
    first = outcomes[0]
    base = await agg.aggregate(
        db=db,
        city_id=city.id,
        primary_icao=city.primary_icao,
        reference_icao=city.reference_icao,
        outcome=first,
        forecast_date=market.event_date,
        is_low_market=False,
        city_lat=float(city.nws_lat) if city.nws_lat is not None else None,
        city_lon=float(city.nws_lon) if city.nws_lon is not None else None,
        city_tz=city.timezone,
        onshore_wind_dir=getattr(city, "onshore_wind_dir", None),
    )
    # Same days_ahead the detector uses — it is also what model_skill keys on.
    days_ahead = (market.event_date - (today or date.today())).days
    age = newest_forecast_age_min(base, now)

    rows = []
    for outcome in outcomes:
        price = base["market_price"] if outcome is first else await agg._latest_price(db, outcome.id)
        signals = signals_for_outcome(base, outcome, price)
        raw, bd = estimate_with_breakdown(
            signals, outcome.bucket_min, outcome.bucket_max,
            days_ahead=days_ahead, bucket_unit=signals["_bucket_unit"],
        )
        if not bd.get("has_forecast_data"):
            # Forecasts are per market, so one bucket without data means none
            # have any. The detector skips these; so does the study.
            return []
        rows.append((outcome, float(raw), bd, price))

    try:
        intraday = await intraday_probabilities(db, city, market, outcomes, base, now)
    except Exception as e:     # the daily estimate is still worth recording
        logger.warning(f"[shadow] intraday estimate failed for market {market.id}: {e}")
        intraday = {}

    priced = [r for r in rows if r[3]]
    scale = normalization_scale([r[1] for r in priced], len(outcomes)) if priced else None

    out = []
    for outcome, raw, bd, price in rows:
        normalised = bool(scale is not None and price)
        out.append(BucketEstimate(
            outcome_id=outcome.id,
            raw_p=raw,
            model_p=float(_prob_clip(raw * scale)) if normalised else raw,
            normalized=normalised,
            market_p=float(price["yes_price"]) if price else None,
            n_sources=bd.get("n_global_det"),
            forecast_high_f=bd.get("forecast_high_f"),
            sigma=bd.get("sigma_used"),
            forecast_age_min=age,
            intraday_p=intraday.get(outcome.id),
        ))
    return out
