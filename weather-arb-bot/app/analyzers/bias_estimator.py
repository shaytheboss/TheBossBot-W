"""Airport warm-bias estimator.

METAR daily highs (the Polymarket resolution price) are systematically
warmer than gridded NWP point forecasts — the tarmac and urban heat island
effect shows up in the official ASOS reading but not in a 25-km model cell.

This module computes a per-city rolling 14-day bias:
    bias_f = mean(actual_METAR_daily_max - T-1 NWP forecast average)

A positive bias_f means the airport runs warmer than the models predict,
so we shift each model's point forecast UP by bias_f before computing
P(in bucket). This corrects the systematic under-prediction and prevents
the NO side from appearing overconfidently safe.

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


async def get_station_bias(
    db: AsyncSession,
    city_id: int,
    icao: str,
    window_days: int = WINDOW_DAYS,
) -> dict:
    """Compute rolling airport warm bias for a given ICAO / city.

    Returns a dict:
        bias_f      float   bias in °F to ADD to model forecasts before CDF
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
                    AVG(f.predicted_high_f) AS avg_predicted_f
                FROM (
                    SELECT
                        DATE(observed_at AT TIME ZONE 'UTC') AS fc_date,
                        MAX(temperature_f)                   AS actual_max_f
                    FROM metar_observations
                    WHERE icao = :icao
                      AND observed_at >= :start
                      AND observed_at <  :end_excl
                    GROUP BY DATE(observed_at AT TIME ZONE 'UTC')
                    HAVING COUNT(*) >= 8
                ) metar
                JOIN forecasts f
                  ON f.city_id            = :city_id
                 AND f.forecast_for_date  = metar.fc_date
                 AND f.source             = ANY(:sources)
                 AND DATE(f.retrieved_at AT TIME ZONE 'UTC') =
                         (metar.fc_date - INTERVAL '1 day')::date
                WHERE f.predicted_high_f IS NOT NULL
                GROUP BY metar.fc_date, metar.actual_max_f
                HAVING AVG(f.predicted_high_f) IS NOT NULL
            """),
            {
                "icao": icao,
                "city_id": city_id,
                "start": start,
                "end_excl": end + timedelta(days=1),
                "sources": list(_NWP_SOURCES),
            },
        )
        rows = result.fetchall()
    except Exception as exc:
        logger.warning("Bias query failed for %s city_id=%s: %s", icao, city_id, exc)
        return _default_bias("query error")

    samples = [
        float(r.actual_max_f) - float(r.avg_predicted_f)
        for r in rows
        if r.actual_max_f is not None and r.avg_predicted_f is not None
    ]

    if len(samples) < MIN_SAMPLES:
        return _default_bias(f"only {len(samples)} day(s) of data (need {MIN_SAMPLES})")

    bias = mean(samples)

    if abs(bias) > MAX_BIAS_ABS_F:
        logger.warning(
            "Bias outlier for %s: %.2f°F > %.1f°F cap — using default", icao, bias, MAX_BIAS_ABS_F
        )
        return _default_bias(f"outlier {bias:.2f}°F capped")

    stddev = pstdev(samples)
    return {
        "bias_f": round(bias, 2),
        "samples": len(samples),
        "notes": f"{window_days}d rolling, {len(samples)} samples, σ={stddev:.2f}°F",
        "is_default": False,
    }


def _default_bias(reason: str) -> dict:
    return {
        "bias_f": DEFAULT_BIAS_F,
        "samples": 0,
        "notes": f"default +{DEFAULT_BIAS_F}°F prior ({reason})",
        "is_default": True,
    }
