"""Airport warm-bias estimator.

METAR daily highs (the Polymarket resolution price) are systematically
warmer than gridded NWP point forecasts — the tarmac and urban heat island
effect shows up in the official ASOS reading but not in a 25-km model cell.

This module computes a per-city rolling 14-day bias:
    bias_f = mean(actual_METAR_daily_max - T-1 NWP forecast average)

plus a PER-SOURCE bias for each forecast model individually — GFS and HRRR
have very different systematic errors, and Wunderground (station-anchored)
typically has none, so a single averaged correction over- or under-corrects
individual models. The probability estimator prefers the per-source value
and falls back to the overall bias.

A positive bias_f means the airport runs warmer than the models predict,
so we shift each model's point forecast UP by bias_f before computing
P(in bucket). This corrects the systematic under-prediction and prevents
the NO side from appearing overconfidently safe.

Daily maxima are grouped by the CITY'S LOCAL day (City.timezone), not the
UTC day — a UTC day window includes the previous local evening for US
cities (and is hours off for Asian ones), contaminating the daily max with
the prior afternoon's warmth.

Default prior: +1.5°F (conservative estimate for typical CONUS airports).
"""

import logging
from datetime import date, timedelta
from statistics import mean, pstdev
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DEFAULT_BIAS_F = 1.5
MIN_SAMPLES = 5
WINDOW_DAYS = 14
MAX_BIAS_ABS_F = 8.0  # outlier guard — beyond this, assume a data bug

_NWP_SOURCES = ("gfs", "ecmwf", "hrrr", "nws", "tomorrowio", "meteosource", "icon")
# Per-source bias is also learned for Wunderground (its bias is naturally ~0
# because it is station-anchored); it is excluded from the OVERALL average so
# the headline bias keeps its original "NWP grid vs airport" meaning.
_BIAS_SOURCES = _NWP_SOURCES + ("wunderground",)


async def get_station_bias(
    db: AsyncSession,
    city_id: int,
    icao: str,
    window_days: int = WINDOW_DAYS,
    tz_name: str = "UTC",
) -> dict:
    """Compute rolling airport warm bias for a given ICAO / city.

    Returns a dict:
        bias_f      float   overall bias in °F to ADD to model forecasts before CDF
        per_source  dict    source name → its own bias_f (only sources with
                            enough samples and a sane magnitude appear)
        samples     int     number of daily matched (METAR, forecast) pairs
        notes       str     human-readable description
        is_default  bool    True when falling back to the +1.5°F prior
    """
    end = date.today() - timedelta(days=1)   # yesterday (complete day)
    start = end - timedelta(days=window_days)

    try:
        result = await db.execute(
            text("""
                SELECT
                    metar.fc_date,
                    metar.actual_max_f,
                    f.source,
                    AVG(f.predicted_high_f) AS predicted_f
                FROM (
                    SELECT
                        DATE(observed_at AT TIME ZONE :tz) AS fc_date,
                        MAX(temperature_f)                 AS actual_max_f
                    FROM metar_observations
                    WHERE icao = :icao
                      AND observed_at >= :start
                      AND observed_at <  :end_excl
                    GROUP BY DATE(observed_at AT TIME ZONE :tz)
                    HAVING COUNT(*) >= 8
                ) metar
                JOIN forecasts f
                  ON f.city_id            = :city_id
                 AND f.forecast_for_date  = metar.fc_date
                 AND f.source             = ANY(:sources)
                 AND DATE(f.retrieved_at AT TIME ZONE :tz) =
                         (metar.fc_date - INTERVAL '1 day')::date
                WHERE f.predicted_high_f IS NOT NULL
                GROUP BY metar.fc_date, metar.actual_max_f, f.source
            """),
            {
                "icao": icao,
                "city_id": city_id,
                "start": start,
                "end_excl": end + timedelta(days=1),
                "sources": list(_BIAS_SOURCES),
                "tz": tz_name or "UTC",
            },
        )
        rows = result.fetchall()
    except Exception as exc:
        logger.warning("Bias query failed for %s city_id=%s: %s", icao, city_id, exc)
        return _default_bias("query error")

    # Per-date NWP predictions (for the overall bias) and per-source error lists.
    nwp_by_date: dict = {}          # fc_date → (actual, [predicted...])
    per_source_errs: dict = {}      # source → [actual - predicted, ...]
    for r in rows:
        if r.actual_max_f is None or r.predicted_f is None:
            continue
        actual = float(r.actual_max_f)
        predicted = float(r.predicted_f)
        err = actual - predicted
        per_source_errs.setdefault(r.source, []).append(err)
        if r.source in _NWP_SOURCES:
            entry = nwp_by_date.setdefault(r.fc_date, (actual, []))
            entry[1].append(predicted)

    samples = [
        actual - (sum(preds) / len(preds))
        for actual, preds in nwp_by_date.values()
        if preds
    ]

    # Per-source biases — kept independently of whether the overall bias has
    # enough data, but each source needs its own MIN_SAMPLES and sanity cap.
    per_source: dict = {}
    for source, errs in per_source_errs.items():
        if len(errs) < MIN_SAMPLES:
            continue
        b = mean(errs)
        if abs(b) > MAX_BIAS_ABS_F:
            logger.warning(
                "Per-source bias outlier for %s/%s: %.2f°F > %.1f°F cap — skipped",
                icao, source, b, MAX_BIAS_ABS_F,
            )
            continue
        per_source[source] = round(b, 2)

    if len(samples) < MIN_SAMPLES:
        out = _default_bias(f"only {len(samples)} day(s) of data (need {MIN_SAMPLES})")
        out["per_source"] = per_source
        return out

    bias = mean(samples)

    if abs(bias) > MAX_BIAS_ABS_F:
        logger.warning(
            "Bias outlier for %s: %.2f°F > %.1f°F cap — using default", icao, bias, MAX_BIAS_ABS_F
        )
        out = _default_bias(f"outlier {bias:.2f}°F capped")
        out["per_source"] = per_source
        return out

    stddev = pstdev(samples)
    return {
        "bias_f": round(bias, 2),
        "per_source": per_source,
        "samples": len(samples),
        "notes": f"{window_days}d rolling, {len(samples)} samples, σ={stddev:.2f}°F",
        "is_default": False,
    }


def _default_bias(reason: str) -> dict:
    return {
        "bias_f": DEFAULT_BIAS_F,
        "per_source": {},
        "samples": 0,
        "notes": f"default +{DEFAULT_BIAS_F}°F prior ({reason})",
        "is_default": True,
    }
