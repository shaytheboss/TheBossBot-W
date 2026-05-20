import logging
import math
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

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


def forecast_sigma_for_lead(days_ahead: Optional[int]) -> float:
    """1-sigma forecast uncertainty (°F) for daily max/min by lead time.

    Calibrated against typical GFS/ECMWF skill at lead time:
    - day 0 (same day): ~1.5°F (intraday obs narrow the spread)
    - day 1: ~2.0°F
    - day 2: ~2.5°F
    - day 3: ~3.0°F  (matches the prior hard-coded value)
    - day 4: ~3.5°F
    - day 5+: 4.0–4.5°F (capped)

    Old code used a flat 3.0°F for every lead time, which made narrow
    same-day buckets (e.g. 72–73°F) look way less likely than they
    actually are when a forecast lands right on top of them.
    """
    if days_ahead is None or days_ahead < 0:
        return 3.0
    return min(4.5, 1.5 + 0.5 * days_ahead)


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
    sigma: float = 3.0,
) -> Optional[float]:
    """P(actual ∈ [bucket_min, bucket_max]) given Gaussian forecast error.

    ±0.5°F half-bin correction so a point forecast at a bucket boundary
    contributes ~25-50%, not 0%/100%.
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
    """Laplace-smoothed fraction of ensemble members in the bucket."""
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
    days_ahead: Optional[int] = None,
) -> Tuple[float, dict]:
    """Estimate P(actual ∈ bucket) with full audit trail.

    days_ahead controls:
    - the Gaussian σ applied to deterministic forecasts (smaller for closer lead times)
    - whether observation-based adjustments are applied (METAR trend, ref wind,
      PIREP). These are based on CURRENT observations near the station and
      only correlate with the daily high for same-day markets. They are
      skipped automatically for days_ahead >= 1 to avoid misleading
      adjustments on multi-day forecasts.
    """
    is_low_market = signals.get("is_low_market", False)
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"
    p50_key = "p50_low_f" if is_low_market else "p50_high_f"

    sigma = forecast_sigma_for_lead(days_ahead)
    observation_skipped = days_ahead is not None and days_ahead >= 1

    breakdown: dict = {
        "is_low_market": bool(is_low_market),
        "days_ahead": int(days_ahead) if days_ahead is not None else None,
        "sigma_used": float(sigma),
        "observation_skipped": bool(observation_skipped),
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

    # ── 1. Deterministic sources ───────────────────────────────────────────
    det_probs = []
    for key, label in _DET_SOURCES:
        val = (signals.get(key) or {}).get(fc_key)
        if val is None:
            continue
        p = _gaussian_bucket_prob(val, bucket_min, bucket_max, sigma=sigma)
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
    wg_p = _gaussian_bucket_prob(wg_val, bucket_min, bucket_max, sigma=sigma)
    if wg_val is not None:
        breakdown["wunderground"] = {
            "value_f": float(wg_val),
            "p_in_bucket": float(wg_p) if wg_p is not None else None,
        }
    breakdown["wg_p"] = float(wg_p) if wg_p is not None else None

    # ── Core blend ───────────────────────────────────────────────────────
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

    # ── Observation-based adjustments — SAME DAY ONLY ───────────────────────────
    # METAR trend, reference station wind, and PIREP are all based on
    # CURRENT observations. They only inform the daily high if the market
    # resolves today. Multi-day markets must rely solely on the forecast
    # blend — today's 5kt south wind tells us nothing about Wednesday.
    if not observation_skipped:
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
    days_ahead: Optional[int] = None,
) -> float:
    """Thin wrapper for callers that only need the final probability."""
    p, _ = estimate_with_breakdown(signals, bucket_min, bucket_max, days_ahead=days_ahead)
    return p
