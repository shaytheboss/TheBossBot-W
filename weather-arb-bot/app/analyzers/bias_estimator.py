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

The window mean is RECENCY-WEIGHTED (EWMA, 5-day half-life) rather than flat:
a heat wave or cold snap shows up in the bias within days instead of being
diluted across the full 14-day window, so the bot stops betting NO on hot
buckets sooner once a warming regime begins. A regime-shift note is attached
when the last 5 days diverge from the full window by more than 1.5°F.

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

# ── Short-term regime adaptation (heat-wave responsiveness) ────────────────────
# A flat 14-day mean takes ~2 weeks to absorb a regime shift: when a heat wave
# starts, the actual-vs-forecast error jumps but the old cool days keep dragging
# the average down, so the bot stays under-corrected (too cold) and keeps
# betting NO on hot buckets that then come in. Instead of a flat mean we use an
# exponentially-recency-weighted mean (EWMA): a sample N days old gets weight
# 0.5**(N / HALF_LIFE). With a 5-day half-life over a 14-day window, the last
# few days carry ~2-3× the weight of the oldest, so a warming (or cooling)
# regime is reflected within days while two weeks of data still anchor it
# against single-day noise. The output is always within [min, max] of the
# samples — it cannot overshoot.
BIAS_RECENCY_HALF_LIFE_DAYS = 5.0
# Window (days, most-recent) used only to surface a regime-shift note for the
# dashboard — it does not change the bias value itself.
BIAS_RECENT_NOTE_WINDOW_DAYS = 5
# Recent vs full-window divergence (°F) above which we flag a regime shift.
BIAS_REGIME_SHIFT_F = 1.5


def _recency_weighted_mean(
    dated_samples: list, half_life_days: float = BIAS_RECENCY_HALF_LIFE_DAYS
) -> float:
    """EWMA over (date, value) pairs — recent days weighted more heavily.

    weight(sample) = 0.5 ** (age_in_days / half_life_days), where age is
    measured from the most recent sample. Returns 0.0 for an empty input.
    The result is bounded by [min(values), max(values)] so it can never
    overshoot the observed data.
    """
    if not dated_samples:
        return 0.0
    newest = max(d for d, _ in dated_samples)
    num = 0.0
    den = 0.0
    for d, v in dated_samples:
        age = (newest - d).days
        w = 0.5 ** (age / half_life_days) if half_life_days > 0 else 1.0
        num += w * v
        den += w
    return num / den if den else 0.0


def _recent_window_mean(dated_samples: list, days: int) -> Optional[float]:
    """Flat mean over the most-recent `days` of (date, value) samples."""
    if not dated_samples:
        return None
    newest = max(d for d, _ in dated_samples)
    recent = [v for d, v in dated_samples if (newest - d).days < days]
    return mean(recent) if recent else None


def _regime_shift_note(dated_samples: list, full_mean: float) -> str:
    """Human-readable flag when recent days diverge from the full window."""
    recent = _recent_window_mean(dated_samples, BIAS_RECENT_NOTE_WINDOW_DAYS)
    if recent is None:
        return ""
    delta = recent - full_mean
    if abs(delta) < BIAS_REGIME_SHIFT_F:
        return ""
    direction = "warming" if delta > 0 else "cooling"
    return (
        f"; ⚠️ {direction} regime: last {BIAS_RECENT_NOTE_WINDOW_DAYS}d "
        f"{recent:+.1f}°F vs {full_mean:+.1f}°F full-window (Δ{delta:+.1f}°F)"
    )

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

        # Dated samples → recency-weighted (EWMA) bias so a heat wave is
        # reflected within days rather than diluted over the full 14-day window.
        wu_dated = [
            (fc_date, wu_max - (sum(preds) / len(preds)))
            for fc_date, (wu_max, preds) in wu_by_date.items()
            if preds
        ]
        wu_samples = len(wu_dated)

        if wu_samples >= MIN_SAMPLES:
            candidate = _recency_weighted_mean(wu_dated)
            if abs(candidate) > MAX_BIAS_ABS_F:
                logger.warning(
                    "WU bias outlier for %s: %.2f°F > %.1f°F cap — ignoring WU path",
                    icao, candidate, MAX_BIAS_ABS_F,
                )
                wu_notes = f"WU outlier {candidate:.2f}°F capped"
            else:
                wu_bias_f = candidate
                wu_flat = mean(v for _, v in wu_dated)
                wu_stddev = pstdev([v for _, v in wu_dated])
                wu_notes = (
                    f"WU-anchored {window_days}d EWMA, "
                    f"{wu_samples} samples, σ={wu_stddev:.2f}°F"
                    + _regime_shift_note(wu_dated, wu_flat)
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

    metar_dated = [
        (fc_date, actual - (sum(preds) / len(preds)))
        for fc_date, (actual, preds) in nwp_by_date.items()
        if preds
    ]
    metar_samples_list = [v for _, v in metar_dated]

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

    metar_bias = _recency_weighted_mean(metar_dated)

    if abs(metar_bias) > MAX_BIAS_ABS_F:
        logger.warning(
            "Bias outlier for %s: %.2f°F > %.1f°F cap — using default",
            icao, metar_bias, MAX_BIAS_ABS_F,
        )
        out = _default_bias(f"outlier {metar_bias:.2f}°F capped")
        out["per_source"] = per_source
        return out

    metar_flat = mean(metar_samples_list)
    metar_stddev = pstdev(metar_samples_list)
    return {
        "bias_f": round(metar_bias, 2),
        "per_source": per_source,
        "samples": len(metar_samples_list),
        "notes": (
            f"METAR-based {window_days}d EWMA, "
            f"{len(metar_samples_list)} samples, σ={metar_stddev:.2f}°F"
            + _regime_shift_note(metar_dated, metar_flat)
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
