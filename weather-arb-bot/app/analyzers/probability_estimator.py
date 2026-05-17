import logging
import math
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Typical 1-3 day ahead daily-high forecast uncertainty (degF, 1-sigma).
# Empirically GFS/ECMWF show RMSE ~2-4degF at 24-72h lead for surface T-max.
_FORECAST_SIGMA_F = 3.0

_PROB_CLIP_LO = 0.03
_PROB_CLIP_HI = 0.97

_DET_SOURCES = (
    ("gfs_forecast", "GFS (global)"),
    ("ecmwf_forecast", "ECMWF"),
    ("hrrr_forecast", "HRRR (3km CONUS)"),
    ("nws_forecast", "NWS (official)"),
    ("tomorrowio_forecast", "Tomorrow.io"),
    ("meteosource_forecast", "Meteosource"),
)


def _bucket_contains(value: float, bucket_min: Optional[int], bucket_max: Optional[int]) -> bool:
    if bucket_min is None and bucket_max is None:
        return False
    if bucket_min is not None and value < bucket_min:
        return False
    if bucket_max is not None and value > bucket_max:
        return False
    return True


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _gaussian_bucket_prob(
    forecast_val: Optional[float],
    bucket_min: Optional[int],
    bucket_max: Optional[int],
    sigma: float = _FORECAST_SIGMA_F,
) -> Optional[float]:
    """P(actual ∈ [bucket_min, bucket_max]) given Gaussian forecast error.

    Uses ±0.5°F half-bin correction so a deterministic forecast right at a
    bucket boundary contributes ~25-50% probability, not 0%/100%.
    """
    if forecast_val is None:
        return None
    lo = (bucket_min - 0.5) if bucket_min is not None else -1e9
    hi = (bucket_max + 0.5) if bucket_max is not None else 1e9
    return _norm_cdf((hi - forecast_val) / sigma) - _norm_cdf((lo - forecast_val) / sigma)


def _ensemble_bucket_prob(
    ensemble_values: list,
    bucket_min: Optional[int],
    bucket_max: Optional[int],
) -> Optional[float]:
    """Laplace-smoothed fraction of ensemble members in the bucket.

    (hits + 0.5)/(n + 1) prevents 0/30 → 0% and 30/30 → 100%.
    """
    if not ensemble_values or len(ensemble_values) < 5:
        return None
    hits = sum(1 for v in ensemble_values if _bucket_contains(v, bucket_min, bucket_max))
    n = len(ensemble_values)
    return (hits + 0.5) / (n + 1)


def _clip(p: float, lo: float = _PROB_CLIP_LO, hi: float = _PROB_CLIP_HI) -> float:
    return max(lo, min(hi, p))


def estimate_with_breakdown(
    signals: dict,
    bucket_min: Optional[int],
    bucket_max: Optional[int],
) -> Tuple[float, dict]:
    """Same blend logic as estimate_true_probability, but also returns the
    intermediate per-source probabilities so the formatter can render an audit
    trail ("How we got to 97% NO").
    """
    is_low_market = signals.get("is_low_market", False)
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"
    p50_key = "p50_low_f" if is_low_market else "p50_high_f"

    breakdown: dict = {
        "is_low_market": bool(is_low_market),
        "deterministic": [],
        "ensemble": None,
        "wunderground": None,
        "det_avg": None,
        "ens_p": None,
        "wg_p": None,
        "blend_before_adjustments": None,
        "adjustments": [],
        "final": None,
    }

    # ── 1. Deterministic sources ───────────────────────────────────────
    det_probs = []
    for key, label in _DET_SOURCES:
        val = (signals.get(key) or {}).get(fc_key)
        if val is None:
            continue
        p = _gaussian_bucket_prob(val, bucket_min, bucket_max)
        if p is None:
            continue
        det_probs.append(p)
        breakdown["deterministic"].append({
            "source": label,
            "value_f": float(val),
            "p_in_bucket": float(p),
        })
    det_p = sum(det_probs) / len(det_probs) if det_probs else None
    breakdown["det_avg"] = float(det_p) if det_p is not None else None

    # ── 2. GFS Ensemble ───────────────────────────────────────────────
    ensemble_fc = signals.get("gfs_ensemble") or {}
    ensemble_vals = ensemble_fc.get(ensemble_key) or []
    ens_p = _ensemble_bucket_prob(ensemble_vals, bucket_min, bucket_max)
    if ens_p is not None:
        hits = sum(1 for v in ensemble_vals if _bucket_contains(v, bucket_min, bucket_max))
        n = len(ensemble_vals)
        breakdown["ensemble"] = {
            "n": n,
            "hits": hits,
            "median_f": ensemble_fc.get(p50_key),
            "raw_pct": round(100 * hits / n, 1) if n else None,
            "smoothed_pct": round(100 * ens_p, 1),
        }
    breakdown["ens_p"] = float(ens_p) if ens_p is not None else None

    # ── 3. Wunderground (soft) ─────────────────────────────────────────
    wg_val = (signals.get("wunderground_forecast") or {}).get(fc_key)
    wg_p = _gaussian_bucket_prob(wg_val, bucket_min, bucket_max)
    if wg_val is not None:
        breakdown["wunderground"] = {
            "value_f": float(wg_val),
            "p_in_bucket": float(wg_p) if wg_p is not None else None,
        }
    breakdown["wg_p"] = float(wg_p) if wg_p is not None else None

    # ── Core blend: 70% ensemble + 30% deterministic, then 90/10 with WG ────────
    if ens_p is not None and det_p is not None:
        p = 0.70 * ens_p + 0.30 * det_p
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

    # ── Soft adjustments ───────────────────────────────────────────────
    p_before = p
    trend = signals.get("metar_trend") or {}
    rate = trend.get("temp_rate_per_hour", 0.0) or 0.0
    current_temp = trend.get("current_temp_f")
    if current_temp is not None and abs(rate) > 0.5:
        projected = current_temp + rate * 3.0
        if _bucket_contains(projected, bucket_min, bucket_max):
            p = min(_PROB_CLIP_HI, p * 1.08)
        elif abs(rate) > 2.0:
            p = max(_PROB_CLIP_LO, p * 0.93)
    if abs(p - p_before) > 1e-6:
        breakdown["adjustments"].append({"name": "METAR trend", "delta": float(p - p_before)})
    p_before = p

    bucket_requires_warmth = bucket_min is not None and bucket_min >= 66
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
        pirep_p = _gaussian_bucket_prob(avg_f, bucket_min, bucket_max, sigma=4.0)
        if pirep_p is not None:
            p = 0.95 * p + 0.05 * pirep_p
    if abs(p - p_before) > 1e-6:
        breakdown["adjustments"].append({"name": "Low-altitude PIREP", "delta": float(p - p_before)})

    final = _clip(p)
    breakdown["final"] = float(final)
    return final, breakdown


def estimate_true_probability(
    signals: dict,
    bucket_min: Optional[int],
    bucket_max: Optional[int],
) -> float:
    """Thin wrapper kept for callers that only need the final probability."""
    p, _ = estimate_with_breakdown(signals, bucket_min, bucket_max)
    return p
