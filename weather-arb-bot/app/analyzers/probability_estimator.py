"""Weather arbitrage probability estimator.

Core question: given today's NWP forecasts, what is the probability that the
METAR daily high on the target date lands inside (or outside) a specific
temperature bucket on Polymarket?

## Architecture

The estimator blends two independent probability streams:

1. **Deterministic (30% weight by default)**
   Up to 7 NWP sources (GFS, ECMWF, HRRR, NWS, Tomorrow.io, Meteosource, ICON/DWD).
   Each source's point forecast is converted to P(in bucket) via a Student-t CDF
   centred at the forecast value with scale σ. Heavier tails than Gaussian = more
   probability leaks across bucket boundaries = less overconfidence.

2. **GFS Ensemble (70% weight)**
   30 perturbed GFS runs. Empirical fraction hitting the bucket (Laplace-smoothed)
   is the ensemble probability. Directly captures multi-modal uncertainty that
   σ-based math cannot.

## Bias correction

METAR daily highs at airports are systematically warmer than gridded NWP forecasts
(tarmac + urban heat island). Every forecast value is shifted up by `bias_f`
(learned from a 14-day rolling window of actual vs forecast errors, defaulting to
+1.5°F) before entering the CDF. This prevents the system from being overconfident
that temperature will stay below an upper bucket threshold.

## Calibration constants

σ_base = 2.5°F same-day, grows +0.5°F/day → capped at 5.5°F (day 6+).
Open-ended buckets ("≥X°F" / "≤X°F") use σ×1.5 — tail events are harder to forecast.
Clip: final P clipped to [3%, 92%] — no single source can claim >92% confidence.

## Sparse-source shrinkage

When fewer than 5 globally-available sources report, confidence blends toward 50%.
Each missing global source contributes 8pp of shrinkage. HRRR and NWS are
CONUS-only and excluded from this count for international cities.
"""
import logging
import math
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_PROB_CLIP_LO = 0.03
_PROB_CLIP_HI = 0.92  # lowered from 0.97 — prevents any signal from claiming >92% confidence

_STUDENT_T_DF = 6  # B4: heavier tails than Gaussian -> more conservative near bucket edges

# Each entry: (signals_key, display_label, available_globally)
# available_globally=False means CONUS-only — these sources will legitimately never
# report for international cities and should NOT be counted as "missing data."
_DET_SOURCES: tuple[tuple[str, str, bool], ...] = (
    ("gfs_forecast",        "GFS (global)",     True),
    ("ecmwf_forecast",      "ECMWF",            True),
    ("hrrr_forecast",       "HRRR (3km CONUS)", False),  # CONUS only — 24-48h horizon
    ("nws_forecast",        "NWS (official)",   False),  # CONUS only — US National WS
    ("tomorrowio_forecast", "Tomorrow.io",       True),
    ("meteosource_forecast","Meteosource",       True),
    ("icon_forecast",       "ICON (DWD)",        True),  # via Open-Meteo, global
)

BOUNDARY_WINDOW_F = 1.5
BOUNDARY_MAX_BLEND = 0.25

SOURCE_SPREAD_THRESHOLD_F = 3.0
SOURCE_SPREAD_MAX_BLEND_F = 6.0
ENSEMBLE_WEIGHT_MIN = 0.40
ENSEMBLE_WEIGHT_BASE = 0.70

STRADDLE_EXTRA_BLEND = 0.10

# Sparse-source shrinkage: blend toward 50% when fewer globally-available sources
# than the baseline report. Only global sources count (HRRR/NWS are CONUS-only
# and are excluded from this count for international cities).
# Baseline = 5 global sources expected (GFS, ECMWF, Tomorrow.io, Meteosource, ICON).
# Each missing global source pulls the blended probability 8pp toward 50%.
# Example: 3/5 reporting → 2 missing → 16% blend toward 50%.
_SPARSE_SOURCE_BASELINE = 5
_SPARSE_SOURCE_SHRINK_PER_MISSING = 0.08   # 8pp per missing global source


def _parse_coord(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    sign = -1.0 if s[-1].upper() in ('S', 'W') else 1.0
    s = s.rstrip('NSEWnsew').strip()
    try:
        return sign * float(s)
    except ValueError:
        return None


def forecast_sigma_for_lead(days_ahead: Optional[int]) -> float:
    # Calibrated against real MAE data: NWP daily-high errors are ~2-3°F same-day,
    # growing to ~5°F by day 5. Prior version (1.5 + 0.5*d) was far too optimistic.
    if days_ahead is None or days_ahead < 0:
        return 4.0
    return min(5.5, 2.5 + 0.5 * days_ahead)


def _bucket_to_f_bounds(
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    unit: str = "F",
) -> Tuple[Optional[float], Optional[float]]:
    """Convert (bucket_min, bucket_max) in native unit to exclusive Fahrenheit
    float bounds suitable for comparison against forecast values (always F).

    For unit='F': applies the existing +/-0.5 deg F half-bin so a forecast at a
    bucket boundary contributes ~25-50%, not 0/100%. Range covered is
    [bmin - 0.5, bmax + 0.5).

    For unit='C': exact conversion of the Celsius integer range. "32 deg C"
    bucket (bmin=32, bmax=32) covers [32, 33)C = [89.6, 91.4)F. No
    half-bin needed since the C->F conversion is already exact.
    """
    if unit == "C":
        f_lo = (bucket_min * 9 / 5 + 32) if bucket_min is not None else None
        f_hi = ((bucket_max + 1) * 9 / 5 + 32) if bucket_max is not None else None
    else:
        f_lo = (bucket_min - 0.5) if bucket_min is not None else None
        f_hi = (bucket_max + 0.5) if bucket_max is not None else None
    return f_lo, f_hi


def boundary_uncertainty_blend(
    forecast_avg: Optional[float],
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    all_source_forecasts: Optional[list] = None,
    unit: str = "F",
) -> tuple:
    if forecast_avg is None:
        return 0.0, None, None

    f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, unit)
    edges = []
    if f_lo is not None:
        edges.append(float(f_lo))
    if f_hi is not None:
        edges.append(float(f_hi))
    if not edges:
        return 0.0, None, None

    dist_avg = min(abs(forecast_avg - e) for e in edges)

    dist_min = dist_avg
    closest_source_f = None
    if all_source_forecasts:
        for f in all_source_forecasts:
            d = min(abs(f - e) for e in edges)
            if d < dist_min:
                dist_min = d
                closest_source_f = f

    if closest_source_f is None:
        closest_source_f = forecast_avg
        closest_source_dist_f = dist_avg
    else:
        closest_source_dist_f = dist_min

    effective_dist = dist_min

    if effective_dist >= BOUNDARY_WINDOW_F:
        return 0.0, closest_source_f, closest_source_dist_f

    blend = BOUNDARY_MAX_BLEND * (1.0 - effective_dist / BOUNDARY_WINDOW_F)
    return blend, closest_source_f, closest_source_dist_f


def _bucket_contains(
    value_f: float,
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    unit: str = "F",
) -> bool:
    """True iff value_f (always in deg F) falls inside the bucket."""
    if bucket_min is None and bucket_max is None:
        return False
    if unit == "C":
        value = (value_f - 32.0) * 5.0 / 9.0
        if bucket_min is not None and value < bucket_min:
            return False
        if bucket_max is not None and value >= bucket_max + 1:
            return False
        return True
    if bucket_min is not None and value_f < bucket_min:
        return False
    if bucket_max is not None and value_f > bucket_max:
        return False
    return True


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _gaussian_bucket_prob(
    forecast_val: Optional[float],
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    sigma: float = 3.0,
    unit: str = "F",
) -> Optional[float]:
    """Kept for ensemble sanity-checks; main deterministic path uses Student-t."""
    if forecast_val is None:
        return None
    f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, unit)
    lo = f_lo if f_lo is not None else -1e9
    hi = f_hi if f_hi is not None else 1e9
    return _norm_cdf((hi - forecast_val) / sigma) - _norm_cdf((lo - forecast_val) / sigma)


# --- B4: Student-t heavy tails (pure Python, no scipy) -----------------------

def _betacf(a: float, b: float, x: float) -> float:
    """Lentz continued-fraction for regularized incomplete beta (Numerical Recipes 6.4)."""
    MAXIT = 200
    EPS = 3.0e-7
    FPMIN = 1.0e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    else:
        return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _student_t_cdf(t: float, df: float) -> float:
    """CDF of Student's t(df) evaluated at t."""
    x = df / (df + t * t)
    ibeta = _betai(df / 2.0, 0.5, x)
    return 0.5 * ibeta if t < 0 else 1.0 - 0.5 * ibeta


def _student_t_bucket_prob(
    forecast_val: Optional[float],
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    sigma: float = 3.0,
    df: float = _STUDENT_T_DF,
    unit: str = "F",
) -> Optional[float]:
    """P(actual in bucket) under Student-t(df) centred at forecast_val with scale sigma.

    Heavier tails than Gaussian: when the forecast sits near a bucket edge,
    more probability leaks across it -> more conservative estimates -> less
    overconfidence near resolution boundaries.
    """
    if forecast_val is None:
        return None
    f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, unit)
    lo = f_lo if f_lo is not None else -1e9
    hi = f_hi if f_hi is not None else 1e9
    t_lo = (lo - forecast_val) / sigma
    t_hi = (hi - forecast_val) / sigma
    cdf_hi = _student_t_cdf(t_hi, df) if hi < 1e8 else 1.0
    cdf_lo = _student_t_cdf(t_lo, df) if lo > -1e8 else 0.0
    return max(0.0, cdf_hi - cdf_lo)


# -----------------------------------------------------------------------------


def _ensemble_bucket_prob(
    ensemble_values: list,
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    unit: str = "F",
) -> Optional[float]:
    if not ensemble_values or len(ensemble_values) < 5:
        return None
    hits = sum(
        1 for v in ensemble_values if _bucket_contains(v, bucket_min, bucket_max, unit)
    )
    n = len(ensemble_values)
    return (hits + 0.5) / (n + 1)


def _clip(p: float, lo: float = _PROB_CLIP_LO, hi: float = _PROB_CLIP_HI) -> float:
    return max(lo, min(hi, p))


def estimate_with_breakdown(
    signals: dict,
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    days_ahead: Optional[int] = None,
    bucket_unit: str = "F",
) -> Tuple[float, dict]:
    is_low_market = signals.get("is_low_market", False)
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"
    p50_key = "p50_low_f" if is_low_market else "p50_high_f"

    sigma = forecast_sigma_for_lead(days_ahead)
    observation_skipped = days_ahead is not None and days_ahead >= 1

    # Open-ended buckets ("X or higher" / "X or lower") represent tail events where
    # forecast errors are systematically larger than for bounded ranges.
    # Apply a 1.5x sigma multiplier so the model is appropriately less certain.
    is_open_ended = (bucket_min is None) != (bucket_max is None)
    if is_open_ended:
        sigma = sigma * 1.5

    # Airport warm-bias correction: actual METAR daily highs are systematically
    # warmer than gridded NWP point forecasts (runway/urban heat island effect).
    # We shift every model's point forecast UP by bias_f before computing P(in
    # bucket), so the system doesn't overestimate the probability that the
    # temperature stays below an upper bucket bound.
    station_bias = signals.get("station_bias") or {}
    bias_f = float(station_bias.get("bias_f") or 1.5)

    breakdown: dict = {
        "is_low_market": bool(is_low_market),
        "days_ahead": int(days_ahead) if days_ahead is not None else None,
        "sigma_used": float(sigma),
        "is_open_ended": is_open_ended,
        "student_t_df": _STUDENT_T_DF,
        "observation_skipped": bool(observation_skipped),
        "bucket_unit": bucket_unit,
        "deterministic": [],
        "ensemble": None,
        "wunderground": None,
        "det_avg": None,
        "ens_p": None,
        "wg_p": None,
        "blend_before_adjustments": None,
        "boundary_risk": None,
        "model_disagreement": None,
        "straddle_info": None,
        "adjustments": [],
        "forecast_std_dev_f": None,
        "ci_pp": None,
        "final": None,
        "bias_correction": {
            "bias_f": bias_f,
            "samples": int(station_bias.get("samples") or 0),
            "notes": str(station_bias.get("notes") or ""),
            "is_default": bool(station_bias.get("is_default", True)),
        },
    }

    # Which sources have no API key (passed in via signals from signal_aggregator)
    unavailable_api: set[str] = set(signals.get("_unavailable_api") or [])

    det_probs: list[float] = []
    det_vals: list[float] = []
    n_global_det = 0          # count of global (non-CONUS-only) sources with data
    missing_sources: list[str] = []        # global sources with no data
    missing_no_key: list[str] = []         # global sources whose API key is unconfigured
    missing_conus_only: list[str] = []     # CONUS-only sources (legitimately absent for intl cities)

    for key, label, is_global in _DET_SOURCES:
        src_data = signals.get(key) or {}
        val = src_data.get(fc_key)
        if val is None:
            if not is_global:
                missing_conus_only.append(label)
            elif key in unavailable_api:
                missing_no_key.append(label)
            else:
                missing_sources.append(label)
            continue
        corrected_val = float(val) + bias_f
        p = _student_t_bucket_prob(corrected_val, bucket_min, bucket_max, sigma=sigma, unit=bucket_unit)
        if p is None:
            if not is_global:
                missing_conus_only.append(label)
            else:
                missing_sources.append(label)
            continue
        if is_global:
            n_global_det += 1
        det_probs.append(p)
        det_vals.append(float(corrected_val))
        det_entry: dict = {
            "source": label,
            "value_f": float(corrected_val),
            "raw_value_f": float(val),
            "p_in_bucket": float(p),
        }
        lat_val = _parse_coord(src_data.get("used_lat"))
        if lat_val is not None:
            det_entry["used_lat"] = lat_val
        lon_val = _parse_coord(src_data.get("used_lon"))
        if lon_val is not None:
            det_entry["used_lon"] = lon_val
        breakdown["deterministic"].append(det_entry)
    det_p = sum(det_probs) / len(det_probs) if det_probs else None
    n_det = len(det_probs)
    breakdown["det_avg"] = float(det_p) if det_p is not None else None
    breakdown["missing_sources"] = missing_sources          # global, no data
    breakdown["missing_no_key"] = missing_no_key            # global, API key not configured
    breakdown["missing_conus_only"] = missing_conus_only    # CONUS-only, N/A for intl cities

    ensemble_fc = signals.get("gfs_ensemble") or {}
    raw_ensemble_vals = ensemble_fc.get(ensemble_key) or []
    # Shift all ensemble members by the same bias — they have the same systematic
    # cold bias as the deterministic GFS run.
    ensemble_vals = [v + bias_f for v in raw_ensemble_vals] if raw_ensemble_vals else []
    raw_p50 = ensemble_fc.get(p50_key)
    biased_p50 = (float(raw_p50) + bias_f) if raw_p50 is not None else None
    ens_p = _ensemble_bucket_prob(ensemble_vals, bucket_min, bucket_max, unit=bucket_unit)
    if ens_p is not None:
        hits = sum(
            1 for v in ensemble_vals
            if _bucket_contains(v, bucket_min, bucket_max, unit=bucket_unit)
        )
        n = len(ensemble_vals)
        breakdown["ensemble"] = {
            "n": n,
            "hits": hits,
            "median_f": biased_p50,
            "raw_pct": round(100 * hits / n, 1) if n else None,
            "smoothed_pct": round(100 * ens_p, 1),
        }
    breakdown["ens_p"] = float(ens_p) if ens_p is not None else None

    wg_src = signals.get("wunderground_forecast") or {}
    wg_val = wg_src.get(fc_key)
    wg_corrected = (float(wg_val) + bias_f) if wg_val is not None else None
    wg_p = _student_t_bucket_prob(wg_corrected, bucket_min, bucket_max, sigma=sigma, unit=bucket_unit)
    if wg_val is not None:
        wg_entry: dict = {
            "value_f": float(wg_corrected),
            "raw_value_f": float(wg_val),
            "p_in_bucket": float(wg_p) if wg_p is not None else None,
        }
        lat_val = _parse_coord(wg_src.get("used_lat"))
        if lat_val is not None:
            wg_entry["used_lat"] = lat_val
        lon_val = _parse_coord(wg_src.get("used_lon"))
        if lon_val is not None:
            wg_entry["used_lon"] = lon_val
        breakdown["wunderground"] = wg_entry
    breakdown["wg_p"] = float(wg_p) if wg_p is not None else None

    # Whether ANY real forecast source contributed. When False the blend falls
    # back to a flat 0.25 prior, which must NOT be turned into a recommendation.
    breakdown["has_forecast_data"] = bool(
        det_p is not None or ens_p is not None or wg_p is not None
    )

    all_source_forecasts = list(det_vals)
    if wg_corrected is not None:
        all_source_forecasts.append(float(wg_corrected))

    # F2: compute forecast std dev and confidence interval across sources
    if len(all_source_forecasts) >= 2:
        mean_f = sum(all_source_forecasts) / len(all_source_forecasts)
        var_f = sum((f - mean_f) ** 2 for f in all_source_forecasts) / (len(all_source_forecasts) - 1)
        std_dev_f = math.sqrt(var_f)
        breakdown["forecast_std_dev_f"] = round(std_dev_f, 2)
        p_ci_hi = _student_t_bucket_prob(
            mean_f + std_dev_f, bucket_min, bucket_max, sigma=sigma, unit=bucket_unit
        ) or 0.0
        p_ci_lo = _student_t_bucket_prob(
            mean_f - std_dev_f, bucket_min, bucket_max, sigma=sigma, unit=bucket_unit
        ) or 0.0
        breakdown["ci_pp"] = round(abs(p_ci_hi - p_ci_lo) / 2.0 * 100.0, 1)

    ensemble_weight = ENSEMBLE_WEIGHT_BASE
    det_weight = 1.0 - ensemble_weight

    if len(all_source_forecasts) >= 2:
        min_source_f = min(all_source_forecasts)
        max_source_f = max(all_source_forecasts)
        source_spread = max_source_f - min_source_f

        if source_spread > SOURCE_SPREAD_THRESHOLD_F:
            excess = min(
                source_spread - SOURCE_SPREAD_THRESHOLD_F,
                SOURCE_SPREAD_MAX_BLEND_F - SOURCE_SPREAD_THRESHOLD_F,
            )
            reduction = (
                (excess / (SOURCE_SPREAD_MAX_BLEND_F - SOURCE_SPREAD_THRESHOLD_F))
                * (ENSEMBLE_WEIGHT_BASE - ENSEMBLE_WEIGHT_MIN)
            )
            ensemble_weight = ENSEMBLE_WEIGHT_BASE - reduction
        else:
            source_spread = 0.0 if len(all_source_forecasts) < 2 else source_spread

        breakdown["model_disagreement"] = {
            "source_spread_f": round(source_spread, 2),
            "ensemble_weight_used": round(ensemble_weight, 4),
            "det_weight_used": round(1.0 - ensemble_weight, 4),
            "threshold_f": SOURCE_SPREAD_THRESHOLD_F,
        }
        det_weight = 1.0 - ensemble_weight
    else:
        source_spread = 0.0

    if ens_p is not None and det_p is not None:
        p = ensemble_weight * ens_p + det_weight * det_p
    elif ens_p is not None:
        p = ens_p
    elif det_p is not None:
        p = det_p
    elif wg_p is not None:
        p = wg_p
    else:
        p = 0.25
    if wg_p is not None and (ens_p is not None or det_p is not None):
        p = 0.90 * p + 0.10 * wg_p
    breakdown["blend_before_adjustments"] = float(p)

    fc_for_boundary: Optional[float] = None
    if det_vals:
        fc_for_boundary = sum(det_vals) / len(det_vals)
    elif wg_corrected is not None:
        fc_for_boundary = float(wg_corrected)

    if fc_for_boundary is not None:
        blend_w, closest_source_f, closest_source_dist_f = boundary_uncertainty_blend(
            fc_for_boundary, bucket_min, bucket_max,
            all_source_forecasts=all_source_forecasts if all_source_forecasts else None,
            unit=bucket_unit,
        )
        if blend_w > 0:
            p_before = p
            p = (1.0 - blend_w) * p + blend_w * 0.5

            f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)
            breakdown["boundary_risk"] = {
                "avg_forecast_f": float(fc_for_boundary),
                "closest_source_f": float(closest_source_f) if closest_source_f is not None else None,
                "closest_source_dist_f": float(closest_source_dist_f) if closest_source_dist_f is not None else None,
                "blend_weight": float(blend_w),
                "bucket_true_min": float(f_lo) if f_lo is not None else None,
                "bucket_true_max": float(f_hi) if f_hi is not None else None,
            }
            breakdown["adjustments"].append({
                "name": "Boundary proximity",
                "delta": float(p - p_before),
            })

    if all_source_forecasts and (bucket_min is not None or bucket_max is not None):
        f_lo_s, f_hi_s = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)

        def _inside(f: float) -> bool:
            if f_lo_s is not None and f < f_lo_s:
                return False
            if f_hi_s is not None and f >= f_hi_s:
                return False
            return True

        sources_inside = [f for f in all_source_forecasts if _inside(f)]
        sources_outside = [f for f in all_source_forecasts if not _inside(f)]
        straddles = len(sources_inside) > 0 and len(sources_outside) > 0

        breakdown["straddle_info"] = {
            "straddles": straddles,
            "inside_sources": [round(f, 2) for f in sources_inside],
            "outside_sources": [round(f, 2) for f in sources_outside],
        }

        if straddles:
            fraction_inside = len(sources_inside) / len(all_source_forecasts)
            straddle_blend = STRADDLE_EXTRA_BLEND * fraction_inside
            p_before = p
            p = p * (1 - straddle_blend) + 0.50 * straddle_blend
            breakdown["adjustments"].append({
                "name": "Straddle blend",
                "delta": float(p - p_before),
            })

    if not observation_skipped:
        # F-equivalent bucket floor for warm-bucket checks
        f_lo_w, _ = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)
        bucket_requires_warmth = f_lo_w is not None and f_lo_w >= 66

        p_before = p
        trend = signals.get("metar_trend") or {}
        rate = trend.get("temp_rate_per_hour", 0.0) or 0.0
        current_temp = trend.get("current_temp_f")
        if current_temp is not None and abs(rate) > 0.5:
            projected = current_temp + rate * 3.0
            if _bucket_contains(projected, bucket_min, bucket_max, unit=bucket_unit):
                p = min(_PROB_CLIP_HI, p * 1.08)
            elif abs(rate) > 2.0:
                p = max(_PROB_CLIP_LO, p * 0.93)
        if abs(p - p_before) > 1e-6:
            breakdown["adjustments"].append({"name": "METAR trend", "delta": float(p - p_before)})
        p_before = p

        ref = signals.get("reference_metar") or {}
        ref_wind_dir = ref.get("wind_direction")
        ref_wind_kt = ref.get("wind_speed_kt", 0) or 0
        if ref_wind_dir is not None and ref_wind_kt > 8:
            onshore = 270 <= ref_wind_dir <= 340
            if onshore and bucket_requires_warmth:
                p *= 0.85
            elif onshore and not bucket_requires_warmth:
                p *= 1.10
            elif not onshore and bucket_requires_warmth:
                p *= 1.10
        if abs(p - p_before) > 1e-6:
            breakdown["adjustments"].append({"name": "Reference wind", "delta": float(p - p_before)})
        p_before = p

        pireps = signals.get("pireps") or []
        low_pireps = [
            r for r in pireps
            if (r.get("flight_level_ft") or 99999) <= 5000
            and r.get("temperature_c") is not None
        ]
        if low_pireps:
            avg_c = sum(r["temperature_c"] for r in low_pireps) / len(low_pireps)
            avg_f = avg_c * 9 / 5 + 32
            pirep_p = _student_t_bucket_prob(
                avg_f, bucket_min, bucket_max, sigma=4.0, unit=bucket_unit
            )
            if pirep_p is not None:
                p = 0.95 * p + 0.05 * pirep_p
        if abs(p - p_before) > 1e-6:
            breakdown["adjustments"].append({"name": "Low-altitude PIREP", "delta": float(p - p_before)})
        p_before = p

        # E2: Dew point convergence -- near-saturated air suppresses daytime max
        primary = signals.get("primary_metar") or {}
        primary_temp_f = primary.get("temperature_f")
        primary_dew_f = primary.get("dew_point_f")
        if (
            primary_temp_f is not None
            and primary_dew_f is not None
            and bucket_requires_warmth
        ):
            dew_spread_f = primary_temp_f - primary_dew_f
            if dew_spread_f < 5.0:
                p = max(_PROB_CLIP_LO, p * 0.92)
                if abs(p - p_before) > 1e-6:
                    breakdown["adjustments"].append({
                        "name": "Dew point convergence",
                        "delta": float(p - p_before),
                    })
                    breakdown["dew_convergence"] = {
                        "spread_f": round(dew_spread_f, 1),
                        "note": "near-saturated air suppresses daytime max",
                    }
                p_before = p

        # E3: Station temperature gradient (sea/lake-breeze proxy)
        # Reference station significantly cooler than primary -> onshore marine air likely
        ref_temp_f = ref.get("temperature_f")
        if (
            primary_temp_f is not None
            and ref_temp_f is not None
            and bucket_requires_warmth
        ):
            gradient_f = primary_temp_f - ref_temp_f
            if gradient_f > 8.0:
                p = max(_PROB_CLIP_LO, p * 0.91)
                if abs(p - p_before) > 1e-6:
                    breakdown["adjustments"].append({
                        "name": "Station gradient (sea/lake-breeze proxy)",
                        "delta": float(p - p_before),
                    })
                    breakdown["station_gradient"] = {
                        "primary_f": round(primary_temp_f, 1),
                        "reference_f": round(ref_temp_f, 1),
                        "gradient_f": round(gradient_f, 1),
                    }
                p_before = p

    # Sparse-source shrinkage — only count globally-available sources.
    # HRRR/NWS are CONUS-only; their absence for an international city is expected
    # and should not penalise confidence. We only shrink when globally-available
    # sources (GFS, ECMWF, Tomorrow.io, Meteosource, ICON) are missing.
    n_global_missing = max(0, _SPARSE_SOURCE_BASELINE - n_global_det)
    if n_global_missing > 0:
        shrink = n_global_missing * _SPARSE_SOURCE_SHRINK_PER_MISSING
        p_before = p
        p = p * (1.0 - shrink) + 0.5 * shrink
        breakdown["adjustments"].append({
            "name": "Sparse sources",
            "delta": float(p - p_before),
        })
        breakdown["sparse_sources"] = {
            "n_global_det": n_global_det,
            "n_det": n_det,
            "baseline": _SPARSE_SOURCE_BASELINE,
            "n_global_missing": n_global_missing,
            "shrink_applied": round(shrink, 3),
        }

    final = _clip(p)
    breakdown["final"] = float(final)
    return final, breakdown


def estimate_true_probability(
    signals: dict,
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    days_ahead: Optional[int] = None,
    bucket_unit: str = "F",
) -> float:
    p, _ = estimate_with_breakdown(
        signals, bucket_min, bucket_max, days_ahead=days_ahead, bucket_unit=bucket_unit
    )
    return p
