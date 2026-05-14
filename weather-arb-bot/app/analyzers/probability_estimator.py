import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _bucket_contains(value: float, bucket_min: Optional[int], bucket_max: Optional[int]) -> bool:
    if bucket_min is None and bucket_max is None:
        return False
    if bucket_min is not None and value < bucket_min:
        return False
    if bucket_max is not None and value > bucket_max:
        return False
    return True


def _ensemble_bucket_prob(
    ensemble_values: list,
    bucket_min: Optional[int],
    bucket_max: Optional[int],
) -> Optional[float]:
    """Compute fraction of ensemble members whose value falls in the bucket."""
    if not ensemble_values or len(ensemble_values) < 5:
        return None
    hits = sum(1 for v in ensemble_values if _bucket_contains(v, bucket_min, bucket_max))
    return hits / len(ensemble_values)


def _forecast_implied_prob(
    forecast_val: Optional[int],
    bucket_min: Optional[int],
    bucket_max: Optional[int],
) -> float:
    """Heuristic probability from a single deterministic forecast value."""
    if forecast_val is None:
        return 0.33
    in_bucket = _bucket_contains(forecast_val, bucket_min, bucket_max)
    if in_bucket:
        return 0.70
    if bucket_max is not None and forecast_val > bucket_max:
        gap = forecast_val - bucket_max
    elif bucket_min is not None and forecast_val < bucket_min:
        gap = bucket_min - forecast_val
    else:
        gap = 0
    return max(0.05, 0.70 - gap * 0.12)


def _clip(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))


def estimate_true_probability(
    signals: dict,
    bucket_min: Optional[int],
    bucket_max: Optional[int],
) -> float:
    is_low_market = signals.get("is_low_market", False)
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"

    # --- Primary source: Open-Meteo GFS ensemble ---
    # 30+ model runs give a proper probabilistic estimate; use directly when available.
    ensemble_fc = signals.get("gfs_ensemble") or {}
    ensemble_vals = ensemble_fc.get(ensemble_key) or []
    ensemble_p = _ensemble_bucket_prob(ensemble_vals, bucket_min, bucket_max)
    if ensemble_p is not None:
        return _clip(ensemble_p)

    # --- Fallback: deterministic model heuristic ---
    wg = signals.get("wunderground_forecast") or {}
    p = _forecast_implied_prob(wg.get(fc_key), bucket_min, bucket_max)

    gfs = signals.get("gfs_forecast") or {}
    ecmwf = signals.get("ecmwf_forecast") or {}
    model_probs = []
    for fc in (gfs, ecmwf):
        v = fc.get(fc_key)
        if v is not None:
            model_probs.append(_forecast_implied_prob(v, bucket_min, bucket_max))

    if model_probs:
        model_p = sum(model_probs) / len(model_probs)
        models_agree = len(model_probs) == 2 and abs(model_probs[0] - model_probs[1]) < 0.15
        p = (0.35 * p + 0.65 * model_p) if models_agree else (0.55 * p + 0.45 * model_p)

    # --- METAR trend adjustment ---
    trend = signals.get("metar_trend") or {}
    rate = trend.get("temp_rate_per_hour", 0.0) or 0.0
    current_temp = trend.get("current_temp_f")
    bucket_requires_warmth = bucket_min is not None and bucket_min >= 66

    if current_temp is not None:
        projected = current_temp + rate * 3.0
        if _bucket_contains(projected, bucket_min, bucket_max):
            p = _clip(p * 1.20)
        elif abs(rate) > 1.5:
            p = _clip(p * 0.85)

    # --- Reference station wind adjustment ---
    ref = signals.get("reference_metar") or {}
    ref_wind_dir = ref.get("wind_direction")
    ref_wind_kt = ref.get("wind_speed_kt", 0) or 0
    if ref_wind_dir is not None and ref_wind_kt > 8:
        onshore = 270 <= ref_wind_dir <= 340
        if onshore and bucket_requires_warmth:
            p = _clip(p * 0.75)
        elif onshore and not bucket_requires_warmth:
            p = _clip(p * 1.15)
        elif not onshore and bucket_requires_warmth:
            p = _clip(p * 1.15)

    # --- Low-altitude PIREP adjustment ---
    pireps = signals.get("pireps") or []
    low_pireps = [
        r for r in pireps
        if (r.get("flight_level_ft") or 99999) <= 5000
        and r.get("temperature_c") is not None
    ]
    if low_pireps:
        avg_c = sum(r["temperature_c"] for r in low_pireps) / len(low_pireps)
        avg_f = avg_c * 9 / 5 + 32
        if avg_f > 65 and bucket_requires_warmth:
            p = _clip(p * 1.12)
        elif avg_f < 55 and bucket_requires_warmth:
            p = _clip(p * 0.88)

    return _clip(p)
