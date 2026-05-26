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

BOUNDARY_WINDOW_F = 1.5
BOUNDARY_MAX_BLEND = 0.25

SOURCE_SPREAD_THRESHOLD_F = 3.0
SOURCE_SPREAD_MAX_BLEND_F = 6.0
ENSEMBLE_WEIGHT_MIN = 0.40
ENSEMBLE_WEIGHT_BASE = 0.70

STRADDLE_EXTRA_BLEND = 0.10


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
    if days_ahead is None or days_ahead < 0:
        return 3.0
    return min(4.5, 1.5 + 0.5 * days_ahead)


def _bucket_to_f_bounds(
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    unit: str = "F",
) -> Tuple[Optional[float], Optional[float]]:
    """Convert (bucket_min, bucket_max) in native unit to exclusive Fahrenheit
    float bounds suitable for comparison against forecast values (always F).

    For unit='F': applies the existing ±0.5°F half-bin so a forecast at a
    bucket boundary contributes ~25-50%, not 0/100%. Range covered is
    [bmin - 0.5, bmax + 0.5).

    For unit='C': exact conversion of the Celsius integer range. "32°C"
    bucket (bmin=32, bmax=32) covers [32, 33)°C = [89.6, 91.4)°F. No
    half-bin needed since the C→F conversion is already exact.
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
    """True iff value_f (always in °F) falls inside the bucket.

    For C buckets, value_f is converted to °C precisely before compare.
    The bucket range covered is [bmin, bmax+1) in native unit, so we use
    a strict < on the upper edge.
    """
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
    if forecast_val is None:
        return None
    f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, unit)
    lo = f_lo if f_lo is not None else -1e9
    hi = f_hi if f_hi is not None else 1e9
    return _norm_cdf((hi - forecast_val) / sigma) - _norm_cdf((lo - forecast_val) / sigma)


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

    breakdown: dict = {
        "is_low_market": bool(is_low_market),
        "days_ahead": int(days_ahead) if days_ahead is not None else None,
        "sigma_used": float(sigma),
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
        "final": None,
    }

    det_probs = []
    det_vals: list[float] = []
    for key, label in _DET_SOURCES:
        src_data = signals.get(key) or {}
        val = src_data.get(fc_key)
        if val is None:
            continue
        p = _gaussian_bucket_prob(val, bucket_min, bucket_max, sigma=sigma, unit=bucket_unit)
        if p is None:
            continue
        det_probs.append(p)
        det_vals.append(float(val))
        det_entry: dict = {
            "source": label,
            "value_f": float(val),
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
    breakdown["det_avg"] = float(det_p) if det_p is not None else None

    ensemble_fc = signals.get("gfs_ensemble") or {}
    ensemble_vals = ensemble_fc.get(ensemble_key) or []
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
            "median_f": ensemble_fc.get(p50_key),
            "raw_pct": round(100 * hits / n, 1) if n else None,
            "smoothed_pct": round(100 * ens_p, 1),
        }
    breakdown["ens_p"] = float(ens_p) if ens_p is not None else None

    wg_src = signals.get("wunderground_forecast") or {}
    wg_val = wg_src.get(fc_key)
    wg_p = _gaussian_bucket_prob(wg_val, bucket_min, bucket_max, sigma=sigma, unit=bucket_unit)
    if wg_val is not None:
        wg_entry: dict = {
            "value_f": float(wg_val),
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

    all_source_forecasts = list(det_vals)
    if wg_val is not None:
        all_source_forecasts.append(float(wg_val))

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
    elif wg_val is not None:
        fc_for_boundary = float(wg_val)

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

        # 'Bucket requires warmth' heuristic still keyed on F-equivalent floor.
        f_lo_w, _ = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)
        bucket_requires_warmth = f_lo_w is not None and f_lo_w >= 66
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
            pirep_p = _gaussian_bucket_prob(
                avg_f, bucket_min, bucket_max, sigma=4.0, unit=bucket_unit
            )
            if pirep_p is not None:
                p = 0.95 * p + 0.05 * pirep_p
        if abs(p - p_before) > 1e-6:
            breakdown["adjustments"].append({"name": "Low-altitude PIREP", "delta": float(p - p_before)})

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
