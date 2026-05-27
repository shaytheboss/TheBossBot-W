from typing import Optional


def compute_confidence(
    signals: dict,
    bucket_min: Optional[int],
    bucket_max: Optional[int],
    bucket_unit: str = "F",
) -> int:
    score = 40

    gfs_high = (signals.get("gfs_forecast") or {}).get("predicted_high_f")
    ecmwf_high = (signals.get("ecmwf_forecast") or {}).get("predicted_high_f")
    wg_high = (signals.get("wunderground_forecast") or {}).get("predicted_high_f")

    all_highs = [h for h in [gfs_high, ecmwf_high, wg_high] if h is not None]
    if len(all_highs) >= 2:
        spread = max(all_highs) - min(all_highs)
        if spread <= 2:
            score += 20
        elif spread <= 5:
            score += 10
        else:
            score -= 10

    trend = signals.get("metar_trend") or {}
    rate = trend.get("temp_rate_per_hour", 0.0) or 0.0
    current_temp = trend.get("current_temp_f")

    # Convert native Celsius bucket_min to Fahrenheit before the warmth check
    # so that e.g. 29°C (=84°F) is correctly treated as a warm bucket (>=66°F),
    # not cold (29 < 66).
    if bucket_unit == "C" and bucket_min is not None:
        bucket_min_f = int(bucket_min * 9 / 5 + 32)
    else:
        bucket_min_f = bucket_min

    bucket_requires_warmth = bucket_min_f is not None and bucket_min_f >= 66

    if current_temp is not None:
        if (rate > 0.5 and bucket_requires_warmth) or (rate < -0.5 and not bucket_requires_warmth):
            score += 15
        elif (rate < -0.5 and bucket_requires_warmth) or (rate > 0.5 and not bucket_requires_warmth):
            score -= 15

    ref = signals.get("reference_metar") or {}
    ref_wind_dir = ref.get("wind_direction")
    ref_wind_kt = ref.get("wind_speed_kt", 0) or 0

    if ref_wind_dir is not None:
        onshore = 270 <= ref_wind_dir <= 340
        if onshore and ref_wind_kt > 8:
            if not bucket_requires_warmth:
                score += 15
            else:
                score -= 10
        elif not onshore and ref_wind_kt > 8:
            if bucket_requires_warmth:
                score += 15

    pireps = signals.get("pireps") or []
    low_pireps = [
        r for r in pireps
        if (r.get("flight_level_ft") or 99999) <= 5000
        and r.get("temperature_c") is not None
    ]
    if low_pireps:
        avg_c = sum(r["temperature_c"] for r in low_pireps) / len(low_pireps)
        avg_f = avg_c * 9 / 5 + 32
        if (avg_f > 65 and bucket_requires_warmth) or (avg_f < 60 and not bucket_requires_warmth):
            score += 10
        elif (avg_f < 55 and bucket_requires_warmth) or (avg_f > 68 and not bucket_requires_warmth):
            score -= 5

    price_info = signals.get("market_price") or {}
    yes_price = price_info.get("yes_price")
    if yes_price is not None:
        if 0.05 <= yes_price <= 0.95:
            score += 10

    return max(0, min(100, score))
