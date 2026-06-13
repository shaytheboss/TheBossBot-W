"""Airport warm-bias estimator.

Polymarket temperature markets resolve on Weather Underground (WU) station
readings, which run 1-2°F warmer than METAR/ASOS official readings due to
station siting, microclimate, and WU's max-temperature aggregation. Gridded
NWP forecasts (GFS, ECMWF, HRRR, etc.) are calibrated against standard
METAR observations, so there are two layers of bias:

    METAR bias:  mean(actual_METAR_daily_max  - T-1 NWP forecast average)
    WU bias:     mean(WU_observed_daily_high  - T-1 NWP forecast average)

Because Polymarket resolves on WU, the WU-anchored bias is the correct
correction to apply before computing P(in bucket). When WU historical data
is available in the forecasts table (source='wunderground', past dates),
the WU bias is used as the primary bias_f. METAR bias is used as a fallback
when WU data is insufficient.

This module also computes a PER-SOURCE bias for each forecast model
individually — GFS and HRRR have very different systematic errors, and
Wunderground (station-anchored) typically has none, so a single averaged
correction over- or under-corrects individual models. The probability
estimator prefers the per-source value and falls back to the overall bias.

A positive bias_f means the station reads warmer than the models predict,
so we shift each model's point forecast UP by bias_f before computing
P(in bucket). This corrects the systematic under-prediction and prevents
the NO side from appearing overconfidently safe on hot buckets.

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

    Primary path: WU-anchored bias using Weather Underground historical
    observations (source='wunderground' in the forecasts table), since
    Polymarket resolves on WU. Falls back to METAR-based bias when WU
    data is insufficient.

    Returns a dict:
        bias_f      float   overall bias in °F to ADD to model forecasts before CDF
        per_source  dict    source name → its own bias_f (only sources with
                            enough samples and a sane magnitude appear)
        samples     int     number of daily matched pairs used for bias_f
        notes       str     human-readable description
        is_default  bool    True when falling back to the +1.5°F prior
    """
    end = date.today() - timedelta(days=1)   # yesterday (complete day)
    start = end - timedelta(days=window_days)
    tz = tz_name or "UTC"

    # ------------------------------------------------------------------ #
    # 1. METAR query — drives per_source biases and METAR fallback path   #
    # ------------------------------------------------------------------ #
    try:
        metar_result = await db.execute(
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
                "tz": tz,
            },
        )
        metar_rows = metar_result.fetchall()
    except Exception as exc:
        logger.warning("METAR bias query failed for %s city_id=%s: %s", icao, city_id, exc)
        return _default_bias("query error")

    # ------------------------------------------------------------------ #
    # 2. WU-anchored bias query (primary path for Polymarket resolution)  #
    # ------------------------------------------------------------------ #
    wu_bias_f: Optional[float] = None
    wu_samples: int = 0
    wu_notes: str = ""

    try:
        wu_result = await db.execute(
            text("""
                SELECT
                    wu.fc_date,
                    wu.wu_max_f,
                    f.source,
                    AVG(f.predicted_high_f) AS predicted_f
                FROM (
                    SELECT
                        forecast_for_date AS fc_date,
                        MAX(predicted_high_f) AS wu_max_f
                    FROM forecasts
                    WHERE city_id = :city_id
                      AND source = 'wunderground'
                      AND forecast_for_date >= :start
                      AND forecast_for_date < :end_excl
                      AND predicted_high_f IS NOT NULL
                    GROUP BY forecast_for_date
                    HAVING COUNT(*) >= 1
                ) wu
                JOIN forecasts f
                  ON f.city_id           = :city_id
                 AND f.forecast_for_date = wu.fc_date
                 AND f.source            = ANY(:nwp_sources)
                 AND DATE(f.retrieved_at AT TIME ZONE :tz) =
                         (wu.fc_date - INTERVAL '1 day')::date
                WHERE f.predicted_high_f IS NOT NULL
                GROUP BY wu.fc_date, wu.wu_max_f, f.source
            """),
            {
                "city_id": city_id,
                "start": start,
                "end_excl": end + timedelta(days=1),
                "nwp_sources": list(_NWP_SOURCES),
                "tz": tz,
            },
        )
        wu_rows = wu_result.fetchall()

        # Group by fc_date: compute mean(wu_max - mean(nwp_predictions))
        wu_by_date: dict = {}  # fc_date → (wu_max, [nwp_predicted, ...])
        for r in wu_rows:
            if r.wu_max_f is None or r.predicted_f is None:
                continue
            entry = wu_by_date.setdefault(r.fc_date, (float(r.wu_max_f), []))
            entry[1].append(float(r.predicted_f))

        wu_samples_list = [
            wu_max - (sum(preds) / len(preds))
            for wu_max, preds in wu_by_date.values()
            if preds
        ]
        wu_samples = len(wu_samples_list)

        if wu_samples >= MIN_SAMPLES:
            candidate = mean(wu_samples_list)
            if abs(candidate) > MAX_BIAS_ABS_F:
                logger.warning(
                    "WU bias outlier for %s: %.2f°F > %.1f°F cap — ignoring WU path",
                    icao, candidate, MAX_BIAS_ABS_F,
                )
                wu_notes = f"WU outlier {candidate:.2f}°F capped"
            else:
                wu_bias_f = candidate
                wu_stddev = pstdev(wu_samples_list)
                wu_notes = (
                    f"WU-anchored {window_days}d rolling, "
                    f"{wu_samples} samples, σ={wu_stddev:.2f}°F"
                )

    except Exception as exc:
        logger.warning("WU bias query failed for %s city_id=%s: %s", icao, city_id, exc)
        # Non-fatal — fall through to METAR path

    # ------------------------------------------------------------------ #
    # 3. Per-source biases (METAR-based; fine for relative comparison)    #
    # ------------------------------------------------------------------ #
    nwp_by_date: dict = {}       # fc_date → (actual_metar, [predicted...])
    per_source_errs: dict = {}   # source → [actual - predicted, ...]
    for r in metar_rows:
        if r.actual_max_f is None or r.predicted_f is None:
            continue
        actual = float(r.actual_max_f)
        predicted = float(r.predicted_f)
        err = actual - predicted
        per_source_errs.setdefault(r.source, []).append(err)
        if r.source in _NWP_SOURCES:
            entry = nwp_by_date.setdefault(r.fc_date, (actual, []))
            entry[1].append(predicted)

    metar_samples_list = [
        actual - (sum(preds) / len(preds))
        for actual, preds in nwp_by_date.values()
        if preds
    ]

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

    # ------------------------------------------------------------------ #
    # 4. Pick primary bias: WU path if sufficient, else METAR fallback    #
    # ------------------------------------------------------------------ #
    if wu_bias_f is not None:
        # WU-anchored — preferred because Polymarket resolves on WU
        return {
            "bias_f": round(wu_bias_f, 2),
            "per_source": per_source,
            "samples": wu_samples,
            "notes": wu_notes,
            "is_default": False,
        }

    # METAR fallback
    if len(metar_samples_list) < MIN_SAMPLES:
        out = _default_bias(
            f"only {len(metar_samples_list)} day(s) of data (need {MIN_SAMPLES})"
        )
        out["per_source"] = per_source
        return out

    metar_bias = mean(metar_samples_list)

    if abs(metar_bias) > MAX_BIAS_ABS_F:
        logger.warning(
            "Bias outlier for %s: %.2f°F > %.1f°F cap — using default",
            icao, metar_bias, MAX_BIAS_ABS_F,
        )
        out = _default_bias(f"outlier {metar_bias:.2f}°F capped")
        out["per_source"] = per_source
        return out

    metar_stddev = pstdev(metar_samples_list)
    return {
        "bias_f": round(metar_bias, 2),
        "per_source": per_source,
        "samples": len(metar_samples_list),
        "notes": (
            f"METAR-based {window_days}d rolling, "
            f"{len(metar_samples_list)} samples, σ={metar_stddev:.2f}°F"
            + (f" (WU insufficient: {wu_samples} samples)" if wu_samples > 0 else "")
        ),
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
