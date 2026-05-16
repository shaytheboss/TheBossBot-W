import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Typical 1-3 day ahead daily-high forecast uncertainty (°F, 1-sigma).
# Empirically GFS/ECMWF show RMSE ≈ 2-4°F at 24-72h lead for surface T-max.
_FORECAST_SIGMA_F = 3.0

# Hard clip range for the final probability. Caps certainty at 97% (was 99%)
# to prevent overconfident claims from narrow ensemble samples.
_PROB_CLIP_LO = 0.03
_PROB_CLIP_HI = 0.97


def _bucket_contains(value: float, bucket_min: Optional[int], bucket_max: Optional[int]) -> bool:
    if bucket_min is None and bucket_max is None:
        return False
    if bucket_min is not None and value < bucket_min:
        return False
    if bucket_max is not None and value > bucket_max:
        return False
    return True


def _norm_cdf(z: float) -> float:
    """Standard normal CDF via math.erf (no scipy required)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _gaussian_bucket_prob(
    forecast_val: Optional[float],
    bucket_min: Optional[int],
    bucket_max: Optional[int],
    sigma: float = _FORECAST_SIGMA_F,
) -> Optional[float]:
    """P(actual_high ∈ [bucket_min, bucket_max]) given Gaussian forecast error.

    Uses ±0.5°F half-bin correction on bucket edges so discrete integer buckets
    map to a continuous interval (e.g. "88-89°F" becomes [87.5, 89.5]).
    A deterministic forecast right at a bucket boundary then correctly
    contributes ~25-50% probability to that bucket, rather than 0%/100%.
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
    """Laplace-smoothed fraction of ensemble members whose value falls in the bucket.

    Replaces raw hits/n with (hits + 0.5)/(n + 1) so 0/30 returns ~1.6%
    (instead of 0% → 99% NO certainty) and 30/30 returns ~98.4% (not 100%).
    With a narrow 1-2°F bucket and only 30 members, even a true probability of
    10-15% will frequently yield 0 hits by chance, so the raw fraction is a
    miscalibrated estimator.
    """
    if not ensemble_values or len(ensemble_values) < 5:
        return None
    hits = sum(1 for v in ensemble_values if _bucket_contains(v, bucket_min, bucket_max))
    n = len(ensemble_values)
    return (hits + 0.5) / (n + 1)


def _clip(p: float, lo: float = _PROB_CLIP_LO, hi: float = _PROB_CLIP_HI) -> float:
    return max(lo, min(hi, p))


def estimate_true_probability(
    signals: dict,
    bucket_min: Optional[int],
    bucket_max: Optional[int],
) -> float:
    """Estimate P(daily high falls in [bucket_min, bucket_max]).

    Strategy (in priority order, blended when overlapping):
      1. GFS ensemble fraction in bucket, Laplace-smoothed.
      2. Gaussian probability from GFS / ECMWF deterministic forecasts
         (σ = 3°F captures typical 1-3 day forecast uncertainty so a model
         right at the bucket boundary contributes ~25-50% probability).
      3. WunderGround forecast as a tertiary deterministic.
      4. METAR trend, reference-station wind, and low-altitude PIREP
         signals as small adjustments.

    Always clipped to [3%, 97%] — a 30-member ensemble on a 1-2°F bucket
    does not justify 99% certainty.
    """
    is_low_market = signals.get("is_low_market", False)
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"

    # ── 1. Ensemble probability (Laplace-smoothed) ─────────────────────────
    ensemble_fc = signals.get("gfs_ensemble") or {}
    ensemble_vals = ensemble_fc.get(ensemble_key) or []
    ens_p = _ensemble_bucket_prob(ensemble_vals, bucket_min, bucket_max)

    # ── 2. Deterministic Gaussian probability ──────────────────────────────
    gfs_val = (signals.get("gfs_forecast") or {}).get(fc_key)
    ecmwf_val = (signals.get("ecmwf_forecast") or {}).get(fc_key)
    det_probs = [
        p for p in (
            _gaussian_bucket_prob(gfs_val, bucket_min, bucket_max),
            _gaussian_bucket_prob(ecmwf_val, bucket_min, bucket_max),
        ) if p is not None
    ]
    det_p = sum(det_probs) / len(det_probs) if det_probs else None

    # ── 3. WunderGround tertiary ───────────────────────────────────────────
    wg_val = (signals.get("wunderground_forecast") or {}).get(fc_key)
    wg_p = _gaussian_bucket_prob(wg_val, bucket_min, bucket_max)

    # ── Blend ──────────────────────────────────────────────────────────────
    # Ensemble is the proper probabilistic source, but a single deterministic
    # forecast right at a bucket edge carries real information the ensemble
    # may miss with 30 finite samples. 70/30 blend balances both.
    if ens_p is not None and det_p is not None:
        p = 0.70 * ens_p + 0.30 * det_p
    elif ens_p is not None:
        p = ens_p
    elif det_p is not None:
        p = det_p
    elif wg_p is not None:
        p = wg_p
    else:
        p = 0.25  # no data — weak prior, temperature ranges spread

    # WunderGround as a soft 10% correction when primary sources exist
    if wg_p is not None and (ens_p is not None or det_p is not None):
        p = 0.90 * p + 0.10 * wg_p

    # ── METAR trend adjustment ────────────────────────────────────────────
    trend = signals.get("metar_trend") or {}
    rate = trend.get("temp_rate_per_hour", 0.0) or 0.0
    current_temp = trend.get("current_temp_f")
    if current_temp is not None and abs(rate) > 0.5:
        projected = current_temp + rate * 3.0
        if _bucket_contains(projected, bucket_min, bucket_max):
            p = min(_PROB_CLIP_HI, p * 1.08)
        elif abs(rate) > 2.0:
            p = max(_PROB_CLIP_LO, p * 0.93)

    # ── Reference station wind adjustment ─────────────────────────────────
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

    # ── Low-altitude PIREP soft correction ────────────────────────────────
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

    return _clip(p)
