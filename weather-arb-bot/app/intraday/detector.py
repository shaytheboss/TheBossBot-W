"""Intraday opportunity detector — same-day markets, hours-scale horizon.

Fully parallel to (and isolated from) the daily detector:
- reads the same signals via SignalAggregator (read-only reuse)
- writes ONLY to intraday_opportunities
- own thresholds (settings.intraday_*), own dedup state, own alerts

See INTRADAY.md for the strategy.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import pytz
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func as sqlfunc

from app.analyzers.signal_aggregator import SignalAggregator
from app.collectors.polymarket_collector import PolymarketCollector
from app.config import settings
from app.intraday.estimator import (
    DEFAULT_PARAMS,
    IntradayParams,
    estimate_intraday,
    local_decimal_hour,
)
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.metar import MetarObservation
from app.models.intraday import IntradayOpportunity
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)

aggregator = SignalAggregator()
_poly_col = PolymarketCollector()

# Forecast blend weights: HRRR is the best 0-18h US model, NWS updates hourly.
_BLEND_WEIGHTS = {
    "hrrr_forecast": 2.0,
    "nws_forecast": 1.5,
    "gfs_forecast": 1.25,
    "ecmwf_forecast": 1.25,
    "icon_forecast": 1.0,
    "wunderground_forecast": 1.0,
    "tomorrowio_forecast": 1.0,
    "meteosource_forecast": 1.0,
}

# Re-alert (no new DB record) when certainty moved >= 1pp since last alert.
REALERT_CONF_DELTA = 0.01
_realert_date: Optional[date] = None
_last_alerted: dict[tuple, float] = {}   # (outcome_id, side) -> certainty


def _reset_realert_if_new_day() -> None:
    global _realert_date
    today = date.today()
    if _realert_date != today:
        _realert_date = today
        _last_alerted.clear()


def _params_from_settings() -> IntradayParams:
    return IntradayParams(
        start_hour=float(getattr(settings, "intraday_start_hour_local", 10.0)),
        peak_start_hour=float(getattr(settings, "intraday_peak_start_hour", 14.0)),
        peak_end_hour=float(getattr(settings, "intraday_peak_end_hour", 17.0)),
    )


def blended_forecast_high(signals: dict) -> Optional[float]:
    """Weighted mean of today's available deterministic forecast highs."""
    total_w = 0.0
    acc = 0.0
    for key, w in _BLEND_WEIGHTS.items():
        val = (signals.get(key) or {}).get("predicted_high_f")
        if val is not None:
            acc += w * float(val)
            total_w += w
    return acc / total_w if total_w > 0 else None


async def _minutes_since_running_max(
    db: AsyncSession, icao: str, tz, now_utc: datetime
) -> Optional[float]:
    """Minutes since the observation that set today's (local-day) running max."""
    local_midnight = now_utc.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_midnight.astimezone(timezone.utc)
    max_q = await db.execute(
        select(sqlfunc.max(MetarObservation.temperature_f)).where(
            MetarObservation.icao == icao,
            MetarObservation.observed_at >= day_start,
            MetarObservation.observed_at <= now_utc,
        )
    )
    max_temp = max_q.scalar_one_or_none()
    if max_temp is None:
        return None
    ts_q = await db.execute(
        select(sqlfunc.min(MetarObservation.observed_at)).where(
            MetarObservation.icao == icao,
            MetarObservation.observed_at >= day_start,
            MetarObservation.temperature_f == max_temp,
        )
    )
    max_ts = ts_q.scalar_one_or_none()
    if max_ts is None:
        return None
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)
    return (now_utc - max_ts).total_seconds() / 60.0


async def _has_intraday_today(db: AsyncSession, outcome_id: int, side: str) -> bool:
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    q = await db.execute(
        select(IntradayOpportunity.id).where(
            IntradayOpportunity.outcome_id == outcome_id,
            IntradayOpportunity.side == side,
            IntradayOpportunity.detected_at >= today_start,
        ).limit(1)
    )
    return q.scalar_one_or_none() is not None


def _realert_due(outcome_id: int, side: str, certainty: float) -> tuple[bool, Optional[str]]:
    key = (outcome_id, side)
    last = _last_alerted.get(key)
    if last is None:
        _last_alerted[key] = certainty
        return True, None
    delta = certainty - last
    if abs(delta) < REALERT_CONF_DELTA - 1e-9:
        return False, None
    _last_alerted[key] = certainty
    arrow = "↑" if delta > 0 else "↓"
    return True, f"certainty {arrow}{abs(round(delta * 100))}pp"


async def detect_intraday(db: AsyncSession) -> tuple[List[IntradayOpportunity], List[dict]]:
    """Scan same-day markets. Returns (new_opportunities, realert_dicts).

    New opportunities get a full ⚡ alert and a DB record; realerts are
    lightweight updates on already-recorded signals (no new record, so the
    stats stay one-row-per-position).
    """
    if not bool(getattr(settings, "intraday_enabled", True)):
        return [], []

    params = _params_from_settings()
    _reset_realert_if_new_day()

    alert_thresh = float(getattr(settings, "intraday_min_certainty_alert", 0.90))
    buy_thresh = max(alert_thresh, float(getattr(settings, "intraday_min_certainty_buy", 0.94)))
    min_edge = float(getattr(settings, "intraday_min_edge", 0.05))
    max_edge = float(getattr(settings, "intraday_max_edge", 0.40))
    max_spread = float(getattr(settings, "intraday_max_book_spread", 0.10))
    shares = int(getattr(settings, "intraday_shares_per_buy", 5))

    now_utc = datetime.now(timezone.utc)
    found: List[IntradayOpportunity] = []
    realerts: List[dict] = []

    # Same-day is the CITY's local day; the UTC window [-1, +1] covers all zones.
    result = await db.execute(
        select(Market).where(
            Market.resolved == False,
            Market.event_date >= date.today() - timedelta(days=1),
            Market.event_date <= date.today() + timedelta(days=1),
        )
    )
    markets: List[Market] = result.scalars().all()

    for market in markets:
        city_result = await db.execute(select(City).where(City.id == market.city_id))
        city: Optional[City] = city_result.scalar_one_or_none()
        if not city or not city.active:
            continue
        if not bool(getattr(city, "intraday_enabled", True)):
            continue

        try:
            tz = pytz.timezone(city.timezone) if city.timezone else pytz.utc
        except Exception:
            tz = pytz.utc

        if market.event_date != now_utc.astimezone(tz).date():
            continue  # not the city's local today

        loc_hour = local_decimal_hour(now_utc, tz)
        if loc_hour < params.start_hour:
            continue  # morning: the daily bot is the right tool

        outcomes_result = await db.execute(
            select(MarketOutcome).where(MarketOutcome.market_id == market.id)
        )
        outcomes: List[MarketOutcome] = outcomes_result.scalars().all()
        if not outcomes:
            continue

        minutes_since_max = await _minutes_since_running_max(
            db, city.primary_icao, tz, now_utc
        )

        for outcome in outcomes:
            try:
                opp, realert = await _evaluate_intraday_outcome(
                    db=db, city=city, market=market, outcome=outcome,
                    tz=tz, loc_hour=loc_hour, minutes_since_max=minutes_since_max,
                    params=params, alert_thresh=alert_thresh, buy_thresh=buy_thresh,
                    min_edge=min_edge, max_edge=max_edge, max_spread=max_spread,
                    shares=shares,
                )
                if opp is not None:
                    found.append(opp)
                if realert is not None:
                    realerts.append(realert)
            except Exception as e:
                logger.error(
                    f"Intraday evaluation failed for outcome {outcome.id}: {e}",
                    exc_info=True,
                )

    return found, realerts


async def _evaluate_intraday_outcome(
    db: AsyncSession,
    city: City,
    market: Market,
    outcome: MarketOutcome,
    tz,
    loc_hour: float,
    minutes_since_max: Optional[float],
    params: IntradayParams,
    alert_thresh: float,
    buy_thresh: float,
    min_edge: float,
    max_edge: float,
    max_spread: float,
    shares: int,
) -> tuple[Optional[IntradayOpportunity], Optional[dict]]:
    signals = await aggregator.aggregate(
        db=db,
        city_id=city.id,
        primary_icao=city.primary_icao,
        reference_icao=city.reference_icao,
        outcome=outcome,
        forecast_date=market.event_date,
        city_lat=float(city.nws_lat) if city.nws_lat is not None else None,
        city_lon=float(city.nws_lon) if city.nws_lon is not None else None,
        city_tz=city.timezone,
        onshore_wind_dir=getattr(city, "onshore_wind_dir", None),
    )

    running_max = signals.get("metar_today_max_f")
    price_info = signals.get("market_price")
    if running_max is None or not price_info:
        return None, None
    running_max = float(running_max)

    current_temp = (signals.get("primary_metar") or {}).get("temperature_f")
    current_temp = float(current_temp) if current_temp is not None else None

    book = None
    if outcome.token_id:
        try:
            book = await _poly_col.get_book_summary(outcome.token_id)
        except Exception as e:
            logger.debug(f"Intraday book fetch failed for outcome {outcome.id}: {e}")
    if book is None:
        return None, None
    if book["bid"] == 0 and book["ask"] == 0:
        return None, None
    if book["spread"] > max_spread:
        return None, None

    forecast_high = blended_forecast_high(signals)
    bucket_unit = resolve_bucket_unit(outcome)

    prob, breakdown = estimate_intraday(
        running_max_f=running_max,
        current_temp_f=current_temp,
        minutes_since_max=minutes_since_max,
        forecast_high_f=forecast_high,
        local_hour=loc_hour,
        bucket_min=outcome.bucket_min,
        bucket_max=outcome.bucket_max,
        bucket_unit=bucket_unit,
        params=params,
    )

    yes_entry = book["ask"]
    no_entry = round(1.0 - book["bid"], 4)
    if prob >= 0.5:
        side, certainty, entry_cost = "YES", prob, yes_entry
    else:
        side, certainty, entry_cost = "NO", 1.0 - prob, no_entry

    edge = certainty - entry_cost
    if certainty < alert_thresh or edge < min_edge or edge > max_edge:
        return None, None

    if await _has_intraday_today(db, outcome.id, side):
        # Already recorded today — only a lightweight re-alert on >= 1pp move.
        should, note = _realert_due(outcome.id, side, certainty)
        if should and note is not None:
            return None, {
                "city_name": city.name,
                "bucket_label": outcome.bucket_label,
                "side": side,
                "certainty": round(certainty, 4),
                "edge": round(edge, 4),
                "entry_cost": round(entry_cost, 4),
                "change_note": note,
                "breakdown": breakdown,
                "event_date": market.event_date,
            }
        return None, None

    # First record today — register baseline for future re-alerts.
    _realert_due(outcome.id, side, certainty)

    create_buy = certainty >= buy_thresh and not bool(getattr(city, "blacklisted", False))

    intraday_signals = {
        "_intraday": breakdown,
        "_book": book,
        "_entry_cost": float(entry_cost),
        "market_price": price_info,
        "_alert_threshold": alert_thresh,
        "_buy_threshold": buy_thresh,
        "_create_virtual_buy": bool(create_buy),
        "_bucket_unit": bucket_unit,
    }

    opp = IntradayOpportunity(
        outcome_id=outcome.id,
        detected_at=datetime.now(timezone.utc),
        side=side,
        market_price=price_info.get("yes_price", 0.5),
        estimated_true_prob=prob,
        edge=edge,
        confidence_score=int(round(certainty * 100)),
        signals=intraday_signals,
        alert_sent=False,
        local_hour=round(loc_hour, 2),
        hours_to_peak_end=breakdown["hours_to_peak_end"],
        running_max_f=running_max,
        expected_final_max_f=breakdown["expected_final_max_f"],
        sigma_used=breakdown["sigma_used"],
        lock_state=breakdown["lock_state"],
    )
    if create_buy:
        opp.virtual_shares = shares
        opp.virtual_entry_price = float(entry_cost)
        opp.virtual_cost = float(shares) * float(entry_cost)
        opp.virtual_status = "open"
    db.add(opp)
    await db.commit()
    await db.refresh(opp)
    return opp, None
