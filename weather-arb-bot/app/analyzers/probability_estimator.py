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

# Empirical: in the May 20, 2026 retrospective, 5 of 5 losing bets had
# the actual high land within 1.0°F of a bucket edge — i.e. the bot was
# over-confident on NO bets adjacent to the eventual outcome. This window
# (1.5°F) plus the 25% blend toward 50% would have pushed all five back
# under the alert threshold.
BOUNDARY_WINDOW_F = 1.5
BOUNDARY_MAX_BLEND = 0.25

# Model disagreement thresholds — when sources disagree, reduce ensemble weight.
SOURCE_SPREAD_THRESHOLD_F = 3.0   # spreads below this get full ensemble weight
SOURCE_SPREAD_MAX_BLEND_F = 6.0   # at this spread, ensemble weight is at minimum
ENSEMBLE_WEIGHT_MIN = 0.40        # minimum ensemble weight even with huge spread
ENSEMBLE_WEIGHT_BASE = 0.70       # default ensemble weight

# Straddle: when sources sit on opposite sides of the bucket, apply extra blend.
STRADDLE_EXTRA_BLEND = 0.10  # at most 10% additional blend when sources straddle


def _parse_coord(val) -> Optional[float]:
    """Parse a coordinate value that may be a float or a direction-suffixed string.

    Some APIs (e.g. Meteosource) return coordinates as '47.44N' or '97.66W'
    instead of numeric floats.  This helper handles both formats:
      '47.44N'  →  47.44
      '97.66W'  → -97.66
      30.162    →  30.162
    Returns None if the value cannot be parsed.
    """
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
    """1-sigma forecast uncertainty (°F) for daily max/min by lead time.

    Calibrated against typical GFS/ECMWF skill at lead time:
    - day 0 (same day): ~1.5°F (intraday obs narrow the spread)
    - day 1: ~2.0°F
    - day 2: ~2.5°F
    - day 3: ~3.0°F  (matches the prior hard-coded value)
    - day 4: ~3.5°F
    - day 5+: 4.0–4.5°F (capped)
    """
    if days_ahead is None or days_ahead < 0:
        return 3.0
    return min(4.5, 1.5 + 0.5 * days_ahead)


def boundary_uncertainty_blend(
    forecast_avg: Optional[float],
    bucket_min: Optional[int],
    bucket_max: Optional[int],
    all_source_forecasts: Optional[list] = None,
) -> tuple:
    """Blend weight toward 50% when forecast lies near a bucket edge.

    Polymarket convention: bucket is [bucket_min, bucket_max + 1).
    Uses the closest source forecast (not just the average) to the bucket edge,
    so a single outlier source can trigger the boundary premium.

    Returns (blend_weight, closest_source_f, closest_source_dist_f).
    """
    if forecast_avg is None:
        return 0.0, None, None

    # Polymarket bucket: [bucket_min, bucket_max + 1)
    true_bucket_max = (bucket_max + 1.0) if bucket_max is not None else None
    edges = []
    if bucket_min is not None:
        edges.append(float(bucket_min))
    if true_bucket_max is not None:
        edges.append(true_bucket_max)

    if not edges:
        return 0.0, None, None

    # Distance from average
    dist_avg = min(abs(forecast_avg - e) for e in edges)

    # Distance from closest source
    dist_min = dist_avg
    closest_source_f = None
    if all_source_forecasts:
        for f in all_source_forecasts:
            d = min(abs(f - e) for e in edges)
            if d < dist_min:
                dist_min = d
                closest_source_f = f

    # If average is closest, record it
    if closest_source_f is None:
        closest_source_f = forecast_avg
        closest_source_dist_f = dist_avg
    else:
        closest_source_dist_f = dist_min

    # Use the smaller of avg dist and closest-source dist
    effective_dist = dist_min

    if effective_dist >= BOUNDARY_WINDOW_F:
        return 0.0, closest_source_f, closest_source_dist_f

    blend = BOUNDARY_MAX_BLEND * (1.0 - effective_dist / BOUNDARY_WINDOW_F)
    return blend, closest_source_f, closest_source_dist_f


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
        "boundary_risk": None,
        "model_disagreement": None,
        "straddle_info": None,
        "adjustments": [],
        "final": None,
    }

    # ── 1. Deterministic sources ──────────────────────────────────────────
    det_probs = []
    det_vals: list[float] = []
    for key, label in _DET_SOURCES:
        src_data = signals.get(key) or {}
        val = src_data.get(fc_key)
        if val is None:
            continue
        p = _gaussian_bucket_prob(val, bucket_min, bucket_max, sigma=sigma)
        if p is None:
            continue
        det_probs.append(p)
        det_vals.append(float(val))
        det_entry: dict = {
            "source": label,
            "value_f": float(val),
            "p_in_bucket": float(p),
        }
        # Carry through API-returned coordinates when available.
        # Use _parse_coord() so string formats like '47.44N' are handled safely.
        lat_val = _parse_coord(src_data.get("used_lat"))
        if lat_val is not None:
            det_entry["used_lat"] = lat_val
        lon_val = _parse_coord(src_data.get("used_lon"))
        if lon_val is not None:
            det_entry["used_lon"] = lon_val
        breakdown["deterministic"].append(det_entry)
    det_p = sum(det_probs) / len(det_probs) if det_probs else None
    breakdown["det_avg"] = float(det_p) if det_p is not None else None

    # ── 2. GFS Ensemble ────────────────────────────────────────────
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

    # ── 3. Wunderground (soft) ─────────────────────────────────
    wg_src = signals.get("wunderground_forecast") or {}
    wg_val = wg_src.get(fc_key)
    wg_p = _gaussian_bucket_prob(wg_val, bucket_min, bucket_max, sigma=sigma)
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

    # ── 1a. Model disagreement: adapt ensemble weight based on source spread ──────
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

    # ── Core blend ────────────────────────────────────────────────
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

    # ── 1b. Boundary-proximity uncertainty premium (closest source, not avg) ───
    fc_for_boundary: Optional[float] = None
    if det_vals:
        fc_for_boundary = sum(det_vals) / len(det_vals)
    elif wg_val is not None:
        fc_for_boundary = float(wg_val)

    if fc_for_boundary is not None:
        blend_w, closest_source_f, closest_source_dist_f = boundary_uncertainty_blend(
            fc_for_boundary, bucket_min, bucket_max,
            all_source_forecasts=all_source_forecasts if all_source_forecasts else None,
        )
        if blend_w > 0:
            p_before = p
            p = (1.0 - blend_w) * p + blend_w * 0.5

            true_bmax = (bucket_max + 1.0) if bucket_max is not None else None
            breakdown["boundary_risk"] = {
                "avg_forecast_f": float(fc_for_boundary),
                "closest_source_f": float(closest_source_f) if closest_source_f is not None else None,
                "closest_source_dist_f": float(closest_source_dist_f) if closest_source_dist_f is not None else None,
                "blend_weight": float(blend_w),
                "bucket_true_min": float(bucket_min) if bucket_min is not None else None,
                "bucket_true_max": float(true_bmax) if true_bmax is not None else None,
            }
            breakdown["adjustments"].append({
                "name": "Boundary proximity",
                "delta": float(p - p_before),
            })

    # ── 1c. Straddle detection ───────────────────────────────────────────
    if all_source_forecasts and (bucket_min is not None or bucket_max is not None):
        true_bx = (bucket_max + 1.0) if bucket_max is not None else None

        def _inside(f: float) -> bool:
            below_min = (bucket_min is not None and f < bucket_min)
            if true_bx is not None:
                above_max = (f >= true_bx)
            else:
                above_max = False
            return not below_min and not above_max

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

    # ── Observation-based adjustments — SAME DAY ONLY ────────────────────────
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
