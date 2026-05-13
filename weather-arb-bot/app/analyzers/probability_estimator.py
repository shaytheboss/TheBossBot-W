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


def _forecast_implied_prob(forecast_high: Optional[int], bucket_min: Optional[int], bucket_max: Optional[int]) -> float:
    if forecast_high is None:
        return 0.33
    in_bucket = _bucket_contains(forecast_high, bucket_min, bucket_max)
    if in_bucket:
        return 0.70
    if bucket_max is not None and forecast_high > bucket_max:
        gap = forecast_high - bucket_max
    elif bucket_min is not None and forecast_high < bucket_min:
        gap = bucket_min - forecast_high
    else:
        gap = 0
    return max(0.05, 0.70 - gap * 0.12)


def _clip(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))


def estimate_true_probability(signals: dict, bucket_min: Optional[int], bucket_max: Optional[int]) -> float:
    wg = signals.get("wunderground_forecast") or {}
    p = _forecast_implied_prob(wg.get("predicted_high_f"), bucket_min, bucket_max)

    gfs = signals.get("gfs_forecast") or {}
    ecmwf = signals.get("ecmwf_forecast") or {}
    gfs_high = gfs.get("predicted_high_f")
    ecmwf_high = ecmwf.get("predicted_high_f")

    model_probs = []
    if gfs_high is not None:
        model_probs.append(_forecast_implied_prob(gfs_high, bucket_min, bucket_max))
    if ecmwf_high is not None:
        model_probs.append(_forecast_implied_prob(ecmwf_high, bucket_min, bucket_max))

    if model_probs:
        model_p = sum(model_probs) / len(model_probs)
        models_agree = len(model_probs) == 2 and abs(model_probs[0] - model_probs[1]) < 0.15
        if models_agree:
            p = 0.35 * p + 0.65 * model_p
        else:
            p = 0.55 * p + 0.45 * model_p

    trend = signals.get("metar_trend") or {}
    rate = trend.get("temp_rate_per_hour", 0.0) or 0.0
    current_temp = trend.get("current_temp_f")

    if current_temp is not None and rate is not None:
        projected = current_temp + rate * 3.0
        proj_in_bucket = _bucket_contains(projected, bucket_min, bucket_max)
        if proj_in_bucket:
            p = _clip(p * 1.20)
        elif abs(rate) > 1.5:
            p = _clip(p * 0.85)

    ref = signals.get("reference_metar") or {}
    ref_wind_dir = ref.get("wind_direction")
    ref_wind_kt = ref.get("wind_speed_kt", 0) or 0
    bucket_requires_warmth = bucket_min is not None and bucket_min >= 66

    if ref_wind_dir is not None and ref_wind_kt > 8:
        onshore = 270 <= ref_wind_dir <= 340
        if onshore and bucket_requires_warmth:
            p = _clip(p * 0.75)
        elif onshore and not bucket_requires_warmth:
            p = _clip(p * 1.15)

    pireps = signals.get("pireps") or []
    low_level_pireps = [
        r for r in pireps
        if r.get("flight_level_ft") is not None and r["flight_level_ft"] <= 5000
        and r.get("temperature_c") is not None
    ]

    if low_level_pireps:
        avg_temp_c = sum(r["temperature_c"] for r in low_level_pireps) / len(low_level_pireps)
        avg_temp_f = avg_temp_c * 9 / 5 + 32
        if avg_temp_f > 65 and bucket_requires_warmth:
            p = _clip(p * 1.12)
        elif avg_temp_f < 55 and bucket_requires_warmth:
            p = _clip(p * 0.88)

    return _clip(p)
