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

σ_base = 4.0°F same-day, grows +0.5°F/day → capped at 6.0°F (day 4+).
Calibrated against realized summer daily-MAXIMUM errors (3-5°F even same-day);
the older 2.5°F floor implied ~93% confidence on a 4°F-off forecast.
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

# Maps a signals forecast key to its source name in the forecasts table, so the
# per-source bias learned by bias_estimator can be applied to the right model.
_KEY_TO_BIAS_SOURCE: dict[str, str] = {
    "gfs_forecast": "gfs",
    "ecmwf_forecast": "ecmwf",
    "hrrr_forecast": "hrrr",
    "nws_forecast": "nws",
    "tomorrowio_forecast": "tomorrowio",
    "meteosource_forecast": "meteosource",
    "icon_forecast": "icon",
    "wunderground_forecast": "wunderground",
}

# Ensemble-spread sigma bounds: the blended sigma is clamped to this range so a
# freak ensemble (collapsed or exploded) can't push the CDF into nonsense.
# Floor raised 2.0 → 3.0: on a synoptically quiet summer day every member
# agrees, so the ensemble std collapses toward ~1°F and the blend dragged the
# effective sigma BELOW the lead-time floor — manufacturing false confidence
# precisely when all models are about to be wrong together (the regime the
# ensemble cannot see). A tight ensemble may narrow uncertainty, never below
# the realized same-day error floor.
_SIGMA_BLEND_MIN_F = 3.0
_SIGMA_BLEND_MAX_F = 7.0
# Raw ensembles systematically under-disperse vs realized error; standard
# practice is to inflate the spread before using it as a sigma estimate.
_ENSEMBLE_STD_INFLATION = 1.3
_ENSEMBLE_MIN_FOR_SIGMA = 10   # need at least this many members to trust the spread

# Per-city model weights are clamped so one hot streak can't dominate the blend.
_MODEL_WEIGHT_MIN = 0.5
_MODEL_WEIGHT_MAX = 1.5

# Onshore wind tolerance: a reference-station wind within ±55° of the city's
# configured onshore bearing counts as onshore (sea→land flow).
_ONSHORE_TOLERANCE_DEG = 55


def _source_bias(station_bias: dict, signals_key: str) -> float:
    """Bias (°F) to add to this source's forecast before the CDF.

    Prefers the per-source bias learned by bias_estimator; falls back to the
    overall station bias, then to the +1.5°F prior.
    """
    overall = float((station_bias or {}).get("bias_f") or 1.5)
    per_source = (station_bias or {}).get("per_source") or {}
    src = _KEY_TO_BIAS_SOURCE.get(signals_key)
    if src and src in per_source:
        try:
            return float(per_source[src])
        except (TypeError, ValueError):
            return overall
    return overall


def _effective_sigma(sigma_lead: float, ensemble_vals: list) -> tuple[float, Optional[float]]:
    """Blend the lead-time sigma table with the actual ensemble spread.

    On a synoptically quiet day the ensemble is tight and the fixed table is
    too wide (under-confident); on a volatile day it's too narrow. When enough
    members report, average the two estimates (with standard spread inflation)
    and clamp. Returns (sigma, ensemble_std or None).
    """
    n = len(ensemble_vals)
    if n < _ENSEMBLE_MIN_FOR_SIGMA:
        return sigma_lead, None
    mean_v = sum(ensemble_vals) / n
    var = sum((v - mean_v) ** 2 for v in ensemble_vals) / (n - 1)
    ens_std = math.sqrt(var)
    blended = 0.5 * sigma_lead + 0.5 * (_ENSEMBLE_STD_INFLATION * ens_std)
    return max(_SIGMA_BLEND_MIN_F, min(_SIGMA_BLEND_MAX_F, blended)), round(ens_std, 2)


def _is_onshore(wind_dir: float, onshore_center_deg: float) -> bool:
    """True when wind_dir is within ±_ONSHORE_TOLERANCE_DEG of the onshore bearing."""
    diff = abs((wind_dir - onshore_center_deg + 180.0) % 360.0 - 180.0)
    return diff <= _ONSHORE_TOLERANCE_DEG


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
    # Calibrated against real NWP verification: daily-MAXIMUM errors are larger
    # than daily-MEAN errors — summer heat-wave maxima routinely miss by 3-5°F
    # even same-day, because the timing/intensity of the afternoon peak is the
    # hardest thing to forecast. The previous 2.5°F same-day floor implied ~93%
    # confidence on a 2°F-wide bucket from a 4°F-off forecast — exactly the
    # Denver/Paris overconfidence that produced losses. Floor raised to 4.0°F
    # same-day so a single point forecast can never claim more certainty than
    # the realized error distribution justifies.
    if days_ahead is None or days_ahead < 0:
        return 4.5
    return min(6.0, 4.0 + 0.5 * days_ahead)


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

    observation_skipped = days_ahead is not None and days_ahead >= 1
    is_open_ended = (bucket_min is None) != (bucket_max is None)

    # Airport warm-bias correction: actual METAR daily highs are systematically
    # warmer than gridded NWP point forecasts (runway/urban heat island effect).
    # Each model's point forecast is shifted UP by that model's learned bias
    # (falling back to the overall station bias) before entering the CDF, so the
    # system doesn't overestimate the probability that the temperature stays
    # below an upper bucket bound.
    station_bias = signals.get("station_bias") or {}
    bias_f = float(station_bias.get("bias_f") or 1.5)

    # Pool ensemble members from every available ensemble (GFS + ECMWF), each
    # member shifted by its parent model's learned bias. Pooling reduces
    # dependence on a single modelling centre and gives a larger sample for
    # both the empirical bucket probability and the spread-based sigma below.
    ensemble_key_pairs = (
        ("gfs_ensemble", "gfs_forecast"),
        ("ecmwf_ensemble", "ecmwf_forecast"),
    )
    ensemble_vals: list = []
    ensemble_models: list[str] = []
    for ens_key, parent_key in ensemble_key_pairs:
        fc = signals.get(ens_key) or {}
        members = fc.get(ensemble_key) or []
        if members:
            b = _source_bias(station_bias, parent_key)
            ensemble_vals.extend(float(v) + b for v in members)
            ensemble_models.append(ens_key)

    # Sigma: start from the lead-time table, then blend in the actual ensemble
    # spread when enough members report — a tight ensemble narrows sigma, a
    # volatile one widens it. Open-ended buckets ("X or higher"/"X or lower")
    # are tail events with systematically larger errors → 1.5x multiplier.
    sigma_lead = forecast_sigma_for_lead(days_ahead)
    sigma, ensemble_std_f = _effective_sigma(sigma_lead, ensemble_vals)
    if is_open_ended:
        sigma = sigma * 1.5

    breakdown: dict = {
        "is_low_market": bool(is_low_market),
        "days_ahead": int(days_ahead) if days_ahead is not None else None,
        "sigma_used": float(sigma),
        "sigma_lead": float(sigma_lead),
        "ensemble_std_f": ensemble_std_f,
        "ensemble_models": ensemble_models,
        "is_open_ended": is_open_ended,
        "student_t_df": _STUDENT_T_DF,
        "observation_skipped": bool(observation_skipped),
        "bucket_unit": bucket_unit,
        "deterministic": [],
        "ensemble": None,
        "wunderground": None,
        "det_avg": None,
        # Blended forecast HIGH in °F (bias-corrected). This is the temperature
        # the models expect — distinct from det_avg, which is a PROBABILITY.
        # Used by the near-money detector to find the bucket the forecast lands in.
        "forecast_high_f": None,
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

    # Per-city model weights learned from realized accuracy (signals key → weight,
    # neutral 1.0). Clamped so one model's hot streak can't dominate the blend.
    model_weights: dict = signals.get("model_weights") or {}

    det_probs: list[float] = []
    det_vals: list[float] = []
    det_wp_sum = 0.0          # weighted sum of per-source probabilities
    det_w_sum = 0.0           # sum of weights
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
        corrected_val = float(val) + _source_bias(station_bias, key)
        p = _student_t_bucket_prob(corrected_val, bucket_min, bucket_max, sigma=sigma, unit=bucket_unit)
        if p is None:
            if not is_global:
                missing_conus_only.append(label)
            else:
                missing_sources.append(label)
            continue
        if is_global:
            n_global_det += 1
        try:
            w = float(model_weights.get(key, 1.0))
        except (TypeError, ValueError):
            w = 1.0
        w = max(_MODEL_WEIGHT_MIN, min(_MODEL_WEIGHT_MAX, w))
        det_probs.append(p)
        det_vals.append(float(corrected_val))
        det_wp_sum += w * p
        det_w_sum += w
        det_entry: dict = {
            "source": label,
            "value_f": float(corrected_val),
            "raw_value_f": float(val),
            "p_in_bucket": float(p),
            "weight": round(w, 3),
        }
        lat_val = _parse_coord(src_data.get("used_lat"))
        if lat_val is not None:
            det_entry["used_lat"] = lat_val
        lon_val = _parse_coord(src_data.get("used_lon"))
        if lon_val is not None:
            det_entry["used_lon"] = lon_val
        breakdown["deterministic"].append(det_entry)
    det_p = (det_wp_sum / det_w_sum) if det_w_sum > 0 else None
    n_det = len(det_probs)
    breakdown["det_avg"] = float(det_p) if det_p is not None else None
    breakdown["missing_sources"] = missing_sources          # global, no data
    breakdown["missing_no_key"] = missing_no_key            # global, API key not configured
    breakdown["missing_conus_only"] = missing_conus_only    # CONUS-only, N/A for intl cities

    # ensemble_vals was pooled (and bias-shifted per parent model) above, before
    # sigma was derived from its spread.
    ens_p = _ensemble_bucket_prob(ensemble_vals, bucket_min, bucket_max, unit=bucket_unit)
    if ens_p is not None:
        hits = sum(
            1 for v in ensemble_vals
            if _bucket_contains(v, bucket_min, bucket_max, unit=bucket_unit)
        )
        n = len(ensemble_vals)
        pooled_sorted = sorted(ensemble_vals)
        pooled_median = pooled_sorted[n // 2] if n else None
        breakdown["ensemble"] = {
            "n": n,
            "hits": hits,
            "median_f": round(pooled_median, 1) if pooled_median is not None else None,
            "raw_pct": round(100 * hits / n, 1) if n else None,
            "smoothed_pct": round(100 * ens_p, 1),
            "models": ensemble_models,
        }
    breakdown["ens_p"] = float(ens_p) if ens_p is not None else None

    wg_src = signals.get("wunderground_forecast") or {}
    wg_val = wg_src.get(fc_key)
    # Wunderground gets its own learned bias: it is station-anchored (often the
    # airport itself) so its bias is naturally near zero — the generic NWP warm
    # bias would over-correct it.
    wg_corrected = (
        (float(wg_val) + _source_bias(station_bias, "wunderground_forecast"))
        if wg_val is not None else None
    )
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
    elif ensemble_vals:
        fc_for_boundary = sum(ensemble_vals) / len(ensemble_vals)

    # Expose the blended forecast high so downstream code (near-money detection)
    # can locate the bucket the forecast actually lands in.
    breakdown["forecast_high_f"] = (
        float(fc_for_boundary) if fc_for_boundary is not None else None
    )

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
        # Warm-bucket check uses the NATIVE bucket floor converted to °F — NOT
        # the half-bin-shifted CDF bound from _bucket_to_f_bounds. The half-bin
        # turns a "≥66°F" floor into 65.5, which fails the >=66 test and flips
        # the wind heuristic (an onshore cool wind then BOOSTS the warm bucket
        # instead of dampening it).
        if bucket_min is not None:
            native_floor_f = (
                bucket_min * 9.0 / 5.0 + 32.0 if bucket_unit == "C"
                else float(bucket_min)
            )
        else:
            native_floor_f = None
        bucket_requires_warmth = native_floor_f is not None and native_floor_f >= 66

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
        # Onshore (sea→land) wind suppresses the daytime max. The onshore
        # bearing is city-specific (west coast ≈ 270-340°, Miami ≈ east, etc.)
        # and comes from City.onshore_wind_dir via signals. When the city has
        # no configured bearing the heuristic is skipped entirely — a wrong
        # hardcoded direction is worse than no adjustment.
        onshore_center = signals.get("_onshore_wind_dir")
        if (
            ref_wind_dir is not None and ref_wind_kt > 8
            and onshore_center is not None
        ):
            onshore = _is_onshore(float(ref_wind_dir), float(onshore_center))
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
