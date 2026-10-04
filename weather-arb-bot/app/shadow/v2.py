"""Model v2 — per-city bias correction and accuracy weighting. Record-only.

What it is. For each city, each model and each lead time (days before the
event, as model_skill counts it), the last WINDOW_DAYS settled days give the
model's bias against the measured high (daily_peaks) and how far it misses
once that bias is removed. A bucket's probability is then a normal around
the accuracy-weighted average of the bias-corrected forecasts, with a spread
learned per city from how far that average itself missed.

Why. Learned only from the days before each day (no look-ahead), this put
the forecast in the right bucket 42% of the time one day ahead, against 34%
for the forecast the bot uses today, and 47% vs 39% on the event day
(Jun-Oct, 37 cities whose station matches the market's). Against live
Polymarket prices it was the first version to come out positive — on the
morning of the event day, +1.7 to +4.7 c/share — but over only eight days and
nine configurations, so it is recorded next to the current model and judged
only on days after it starts (/admin/shadow/v2-report).

Cost. The calibration is three small reads per city, once a day, cached;
per snapshot it is arithmetic on signals the study already has. Nothing here
writes; nothing in trading imports it.

Where the city's METAR station differs from the market's resolution station
(see the station audit), "the measured high" is the wrong thermometer and v2
learns the wrong bias — fix the station first.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

WINDOW_DAYS = 21
MAX_LEAD = 2
MIN_SAMPLES = 7
#: Learned spread is widened by this (a mean absolute error understates the
#: tail) and never allowed below the floor: a 0.5°C bucket call is noise.
SIGMA_SCALE = 1.25
SIGMA_FLOOR_F = 0.9
MAE_FLOOR_F = 0.36
#: source in `forecasts` → key in the aggregator's signals
SOURCES = {
    "gfs": "gfs_forecast", "ecmwf": "ecmwf_forecast", "icon": "icon_forecast",
    "meteosource": "meteosource_forecast", "nws": "nws_forecast",
    "hrrr": "hrrr_forecast", "tomorrowio": "tomorrowio_forecast",
    "gfs_ensemble": "gfs_ensemble", "ecmwf_ensemble": "ecmwf_ensemble",
}


@dataclass
class Calibration:
    # (source, lead) → (bias_f, mae_f)
    models: dict = field(default_factory=dict)
    # lead → spread (°F) of the corrected weighted average
    sigma: dict = field(default_factory=dict)


# city_id → (day computed, Calibration)
_CACHE: dict[int, tuple[date, Calibration]] = {}


def reset_cache() -> None:
    _CACHE.clear()


def weighted_mean(corrected: dict[str, float], mae: dict[str, float]) -> Optional[float]:
    if not corrected:
        return None
    w = {s: 1.0 / max(mae[s], MAE_FLOOR_F) ** 2 for s in corrected}
    return sum(corrected[s] * w[s] for s in corrected) / sum(w.values())


def calibrate(truth: dict[date, float], forecasts: dict[tuple, float]) -> Calibration:
    """Pure. truth: {day: measured high °F}; forecasts: {(source, day, lead): °F}."""
    cal = Calibration()
    errs: dict[tuple, list[float]] = {}
    for (src, day, lead), f in forecasts.items():
        if day in truth:
            errs.setdefault((src, lead), []).append(f - truth[day])
    for key, e in errs.items():
        if len(e) >= MIN_SAMPLES:
            bias = sum(e) / len(e)
            cal.models[key] = (bias, sum(abs(x - bias) for x in e) / len(e))
    for lead in range(MAX_LEAD + 1):
        resid = []
        for day, t in truth.items():
            corr = {s: forecasts[(s, day, lead)] - cal.models[(s, lead)][0]
                    for s in SOURCES if (s, day, lead) in forecasts and (s, lead) in cal.models}
            mu = weighted_mean(corr, {s: cal.models[(s, lead)][1] for s in corr})
            if mu is not None:
                resid.append(abs(mu - t))
        if len(resid) >= MIN_SAMPLES:
            cal.sigma[lead] = max(SIGMA_FLOOR_F, SIGMA_SCALE * sum(resid) / len(resid))
    return cal


async def city_calibration(db, city_id: int, today: date) -> Calibration:
    hit = _CACHE.get(city_id)
    if hit and hit[0] == today:
        return hit[1]
    from app.models.forecast import Forecast
    from app.peaks.compute import is_complete
    from app.peaks.models import DailyPeak

    start = today - timedelta(days=WINDOW_DAYS)
    truth = {d: float(mx) for d, mx, n, first, last, gap in (await db.execute(
        select(DailyPeak.local_date, DailyPeak.max_f, DailyPeak.n_obs, DailyPeak.first_obs_hour,
               DailyPeak.last_obs_hour, DailyPeak.max_gap_h)
        .where(DailyPeak.city_id == city_id, DailyPeak.local_date >= start,
               DailyPeak.local_date < today)
    )).all() if is_complete(n, first, last, gap)}
    latest: dict[tuple, tuple] = {}
    if truth:
        for src, day, at, high in (await db.execute(
            select(Forecast.source, Forecast.forecast_for_date, Forecast.retrieved_at,
                   Forecast.predicted_high_f)
            .where(Forecast.city_id == city_id, Forecast.source.in_(list(SOURCES)),
                   Forecast.forecast_for_date.in_(list(truth)),
                   Forecast.predicted_high_f.isnot(None))
        )).all():
            lead = (day - at.date()).days
            if 0 <= lead <= MAX_LEAD:
                k = (src, day, lead)
                if k not in latest or at > latest[k][0]:
                    latest[k] = (at, float(high))
    cal = calibrate(truth, {k: v[1] for k, v in latest.items()})
    _CACHE[city_id] = (today, cal)
    return cal


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_bounds_f(bmin, bmax, unit: str) -> tuple[Optional[float], Optional[float]]:
    """Native bucket → °F interval, edges at ±0.5 of the rounded reading."""
    def f(x):
        return x * 9.0 / 5.0 + 32.0 if unit == "C" else x
    lo = None if bmin is None else f(float(bmin) - 0.5)
    hi = None if bmax is None else f(float(bmax) + 0.5)
    return lo, hi


def mean_and_sigma(cal: Calibration, signals: dict, lead: int) -> tuple[Optional[float], Optional[float]]:
    lead = max(0, min(MAX_LEAD, lead))
    sigma = cal.sigma.get(lead)
    if sigma is None:
        return None, None
    corr, mae = {}, {}
    for src, key in SOURCES.items():
        fc = (signals.get(key) or {}).get("predicted_high_f")
        if fc is None or (src, lead) not in cal.models:
            continue
        bias, err = cal.models[(src, lead)]
        corr[src], mae[src] = float(fc) - bias, err
    return weighted_mean(corr, mae), sigma


def probability(mu: float, sigma: float, lo_f: Optional[float], hi_f: Optional[float]) -> float:
    a = 0.0 if lo_f is None else _phi((lo_f - mu) / sigma)
    b = 1.0 if hi_f is None else _phi((hi_f - mu) / sigma)
    return max(0.0, b - a)


async def v2_for_market(db, city, market, outcomes: list, signals: dict,
                        days_ahead: int, today: date) -> tuple[dict[int, float], Optional[float]]:
    """({outcome_id: P(YES)}, mean °F) or ({}, None) when not calibrated yet."""
    from app.utils.units import resolve_bucket_unit
    cal = await city_calibration(db, city.id, today)
    mu, sigma = mean_and_sigma(cal, signals, days_ahead)
    if mu is None:
        return {}, None
    out = {}
    for o in outcomes:
        lo, hi = bucket_bounds_f(o.bucket_min, o.bucket_max, resolve_bucket_unit(o))
        out[o.id] = probability(mu, sigma, lo, hi)
    return out, mu
