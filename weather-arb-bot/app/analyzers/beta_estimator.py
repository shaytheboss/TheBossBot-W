"""Beta estimator — calibrated parallel probability estimator.

Completely isolated from alpha (probability_estimator.py). Zero imports from
that module — changes there cannot affect beta. Both run simultaneously; each
generates independent recommendations.

Naming convention:
  alpha = classic estimator (probability_estimator.py), unchanged
  beta  = this module: per-city bias-corrected, MAE-sigma, near-miss-weighted

Key improvements vs alpha:
1. Per-city, per-model bias correction using model_skill.bias_f (measured against
   market resolution). bias_f > 0 = model predicted too hot → subtract from forecast.
   Replaces alpha's global +1.5°F station warm-bias correction (which made NY worse).
2. MAE-based per-city sigma: sigma = max(FLOOR, mae_f * SCALE). Accurate models
   get tighter CDFs; noisy models get wider ones automatically.
3. Catastrophic bias blocking: |bias_f| > BETA_BLOCK_BIAS_F (5°F) → exclude that
   (city, model) pair entirely from the blend. Catches LA ECMWF (+6.7°F), etc.
4. Near-miss partial credit in weights: w = 0.7*hit_rate + 0.3*accuracy_score,
   where accuracy_score = 1 - min(1, mae_f/5). Models landing near the bucket
   get rewarded even if binary hit_rate is low (Tokyo Meteosource fix).
5. Variance-city detection (London archetype): high MAE + near-zero bias across
   multiple models → sigma widened 1.2x to prevent overconfidence in noisy cities.
"""
import logging
import math
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

BETA_BLOCK_BIAS_F = 5.0        # block (city, source) when |bias_f| exceeds this
BETA_SIGMA_FLOOR = 4.0         # minimum sigma (°F) for any source (was 3.0 — too tight)
BETA_SIGMA_CAP = 7.0           # maximum sigma (°F)
BETA_MAE_SIGMA_SCALE = 1.2     # per-source sigma = max(floor, mae_f * this)

# ── Calibration fixes (overconfidence was the root cause of beta's losses) ──────
# 1. Bias-correction shrinkage: bias_f from few resolutions is mostly noise, so
#    apply only a fraction of it. fraction = samples / (samples + K). With K=20:
#    n=5 → 20%, n=20 → 50%, n=60 → 75%. Prevents beta "jumping" on noisy bias.
BETA_BIAS_SHRINK_K = 20.0
# 2. Sigma already widened via the raised floor above + bias-estimate uncertainty
#    (mae/sqrt(n)) added in quadrature inside _beta_source_sigma.
# 3. Market-price blend: beta's edge is empirically anti-predictive while the
#    market price tracks realized outcomes far better. Pull beta toward the market
#    by (1 - weight). 0.6 keeps beta the primary voice but reins in the extremes.
BETA_MARKET_BLEND_WEIGHT = 0.6
BETA_ACCURACY_MAE_CEIL = 5.0   # MAE at which accuracy_score = 0.0
BETA_HIT_RATE_WEIGHT = 0.70    # weight of hit_rate in blended weight
BETA_ACCURACY_WEIGHT = 0.30    # weight of accuracy_score in blended weight

BETA_VARIANCE_MAE_MIN = 3.5    # London archetype: high MAE…
BETA_VARIANCE_BIAS_MAX = 1.0   # …but near-zero bias = pure-noise city
BETA_VARIANCE_MIN_SOURCES = 2  # need this many sources qualifying as variance
BETA_SIGMA_VARIANCE_MULT = 1.2

BETA_STUDENT_T_DF = 6

_PROB_CLIP_LO = 0.03
_PROB_CLIP_HI = 0.92

_ENSEMBLE_WEIGHT_BASE = 0.70
_ENSEMBLE_WEIGHT_MIN = 0.40
_ENSEMBLE_STD_INFLATION = 1.3
_ENSEMBLE_MIN_FOR_SIGMA = 10
_SIGMA_BLEND_MIN_F = 4.0
_SIGMA_BLEND_MAX_F = 7.0
_SOURCE_SPREAD_THRESHOLD_F = 3.0
_SOURCE_SPREAD_MAX_BLEND_F = 6.0
_STRADDLE_EXTRA_BLEND = 0.10
_BOUNDARY_WINDOW_F = 1.5
_BOUNDARY_MAX_BLEND = 0.25
_SPARSE_SOURCE_BASELINE = 5
_SPARSE_SOURCE_SHRINK_PER_MISSING = 0.08

# (signals_key, display_label, is_global, source_name_for_model_skill)
_DET_SOURCES: tuple = (
    ("gfs_forecast",         "GFS (global)",      True,  "gfs"),
    ("ecmwf_forecast",       "ECMWF",             True,  "ecmwf"),
    ("hrrr_forecast",        "HRRR (3km CONUS)",  False, "hrrr"),
    ("nws_forecast",         "NWS (official)",    False, "nws"),
    ("tomorrowio_forecast",  "Tomorrow.io",       True,  "tomorrowio"),
    ("meteosource_forecast", "Meteosource",       True,  "meteosource"),
    ("icon_forecast",        "ICON (DWD)",        True,  "icon"),
)

_SIGNALS_KEY_TO_SRC: dict[str, str] = {
    "gfs_forecast": "gfs", "ecmwf_forecast": "ecmwf",
    "hrrr_forecast": "hrrr", "nws_forecast": "nws",
    "tomorrowio_forecast": "tomorrowio", "meteosource_forecast": "meteosource",
    "icon_forecast": "icon", "wunderground_forecast": "wunderground",
}


# ── Pure math helpers (copied, not imported, for full isolation) ──────────────

def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 200, 3.0e-7, 1.0e-30
    qab = a + b; qap = a + 1.0; qam = a - 1.0
    c = 1.0; d = 1.0 - qab * x / qap
    if abs(d) < FPMIN: d = FPMIN
    d = 1.0 / d; h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN: d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        d = 1.0 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN: d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        d = 1.0 / d; delta = d * c; h *= delta
        if abs(delta - 1.0) < EPS: break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0: return 0.0
    if x >= 1.0: return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _student_t_cdf(t: float, df: float) -> float:
    x = df / (df + t * t)
    ibeta = _betai(df / 2.0, 0.5, x)
    return 0.5 * ibeta if t < 0 else 1.0 - 0.5 * ibeta


def _bucket_to_f_bounds(
    bmin: Optional[float], bmax: Optional[float], unit: str = "F"
) -> tuple[Optional[float], Optional[float]]:
    if unit == "C":
        f_lo = (bmin * 9 / 5 + 32) if bmin is not None else None
        f_hi = ((bmax + 1) * 9 / 5 + 32) if bmax is not None else None
    else:
        f_lo = (bmin - 0.5) if bmin is not None else None
        f_hi = (bmax + 0.5) if bmax is not None else None
    return f_lo, f_hi


def _bucket_contains(value_f: float, bmin, bmax, unit: str = "F") -> bool:
    if bmin is None and bmax is None: return False
    if unit == "C":
        value = (value_f - 32.0) * 5.0 / 9.0
        if bmin is not None and value < bmin: return False
        if bmax is not None and value >= bmax + 1: return False
        return True
    if bmin is not None and value_f < bmin: return False
    if bmax is not None and value_f > bmax: return False
    return True


def _student_t_bucket_prob(
    forecast_val: Optional[float], bmin, bmax,
    sigma: float = 3.0, df: float = BETA_STUDENT_T_DF, unit: str = "F",
) -> Optional[float]:
    if forecast_val is None: return None
    f_lo, f_hi = _bucket_to_f_bounds(bmin, bmax, unit)
    lo = f_lo if f_lo is not None else -1e9
    hi = f_hi if f_hi is not None else 1e9
    t_lo = (lo - forecast_val) / sigma
    t_hi = (hi - forecast_val) / sigma
    cdf_hi = _student_t_cdf(t_hi, df) if hi < 1e8 else 1.0
    cdf_lo = _student_t_cdf(t_lo, df) if lo > -1e8 else 0.0
    return max(0.0, cdf_hi - cdf_lo)


def _ensemble_bucket_prob(ensemble_values: list, bmin, bmax, unit: str = "F") -> Optional[float]:
    if not ensemble_values or len(ensemble_values) < 5: return None
    hits = sum(1 for v in ensemble_values if _bucket_contains(v, bmin, bmax, unit))
    n = len(ensemble_values)
    return (hits + 0.5) / (n + 1)


def _clip(p: float) -> float:
    return max(_PROB_CLIP_LO, min(_PROB_CLIP_HI, p))


def _effective_sigma(
    sigma_lead: float, ensemble_vals: list
) -> tuple[float, Optional[float]]:
    n = len(ensemble_vals)
    if n < _ENSEMBLE_MIN_FOR_SIGMA:
        return sigma_lead, None
    mean_v = sum(ensemble_vals) / n
    var = sum((v - mean_v) ** 2 for v in ensemble_vals) / (n - 1)
    ens_std = math.sqrt(var)
    blended = 0.5 * sigma_lead + 0.5 * (_ENSEMBLE_STD_INFLATION * ens_std)
    return max(_SIGMA_BLEND_MIN_F, min(_SIGMA_BLEND_MAX_F, blended)), round(ens_std, 2)


# ── Beta-specific helpers ─────────────────────────────────────────────────────

def _beta_source_bias(skill, signals_key: str, station_bias: dict) -> float:
    """Bias correction (°F) to ADD to a source's raw forecast before the CDF.

    Beta uses model_skill.bias_f measured against market resolution.
    bias_f > 0 = model historically predicted too hot → subtract (correct DOWN).
    This is the opposite sign of alpha's station warm-bias (+1.5°F always added).

    Falls back to the existing station_bias pipeline when no skill data exists,
    so beta is no worse than alpha in the absence of historical data.
    """
    if skill is not None and skill.bias_f is not None and (skill.samples or 0) >= 1:
        # Shrink the correction toward 0 by sample count — a bias_f estimated from
        # few resolutions is dominated by noise, so trust it only partially.
        n = float(skill.samples or 0)
        shrink = n / (n + BETA_BIAS_SHRINK_K)
        return -float(skill.bias_f) * shrink
    # Fallback to per-source station bias, then global bias
    per_source = (station_bias or {}).get("per_source") or {}
    src = _SIGNALS_KEY_TO_SRC.get(signals_key)
    if src and src in per_source:
        try:
            return float(per_source[src])
        except (TypeError, ValueError):
            pass
    return float((station_bias or {}).get("bias_f") or 1.5)


def _beta_source_sigma(skill, sigma_global: float) -> float:
    """Per-source sigma based on historical MAE, decomposed into residual noise.

    When mae ≈ |bias| (pure systematic error, e.g. Ankara ECMWF always cold
    by 4°F), using raw MAE to set sigma double-counts the bias we already
    corrected in _beta_source_bias. Instead we decompose:

        σ_resid = sqrt(max(0, mae² − b_corrected²))

    where b_corrected = bias × shrink (the portion actually removed). This
    drives sigma from the *unpredictable* residual only. London (mae=4°F,
    bias≈0) is unchanged; Ankara (mae≈|bias|) gets a tighter, earned sigma.

    Uncertainty from the uncorrected bias portion and estimation noise are then
    added back in quadrature, so thin-data cities stay conservatively wide.
    """
    if skill is not None and skill.mae_f is not None:
        mae = float(skill.mae_f)
        n = float(skill.samples or 0)

        if skill.bias_f is not None and n >= 1:
            shrink = n / (n + BETA_BIAS_SHRINK_K)
            b_corrected = abs(float(skill.bias_f)) * shrink
            b_uncorrected = abs(float(skill.bias_f)) * (1.0 - shrink)
            sigma_resid = math.sqrt(max(0.0, mae ** 2 - b_corrected ** 2))
        else:
            sigma_resid = mae
            b_uncorrected = 0.0

        mae_sigma = sigma_resid * BETA_MAE_SIGMA_SCALE
        base = max(sigma_global, mae_sigma)

        # Add in quadrature: uncorrected-bias uncertainty + estimation noise.
        # mae/sqrt(n) is the standard error of the bias estimate itself.
        if n >= 1:
            sigma_bias_unc = mae / math.sqrt(n)
            extra = math.sqrt(b_uncorrected ** 2 + sigma_bias_unc ** 2)
            base = math.sqrt(base ** 2 + extra ** 2)

        return max(BETA_SIGMA_FLOOR, min(BETA_SIGMA_CAP, base))
    return sigma_global


def _beta_source_weight(skill) -> float:
    """Blended weight: 70% hit_rate + 30% accuracy-from-MAE.

    Near-miss partial credit: a model that consistently lands just outside the
    bucket gets accuracy_score > 0 even though hit_rate is low. Fixes the
    binary-penalty problem (Tokyo Meteosource: MAE=0.91°F, weight=0.767 in alpha).
    """
    if skill is None or skill.samples is None or skill.samples < 5:
        return 1.0
    hit_score = (float(skill.hits) + 1) / (float(skill.samples) + 2)
    if skill.mae_f is not None:
        accuracy_score = 1.0 - min(1.0, float(skill.mae_f) / BETA_ACCURACY_MAE_CEIL)
    else:
        accuracy_score = hit_score
    combined = BETA_HIT_RATE_WEIGHT * hit_score + BETA_ACCURACY_WEIGHT * accuracy_score
    return max(0.5, min(1.5, 0.5 + combined))


def _is_blocked(skill) -> bool:
    """True when this (city, source) pair has catastrophic systematic bias."""
    return (
        skill is not None
        and skill.bias_f is not None
        and abs(float(skill.bias_f)) > BETA_BLOCK_BIAS_F
    )


def _is_variance_city(city_skill: dict) -> bool:
    """London archetype: multiple sources show high MAE + near-zero bias.

    These cities have inherently unpredictable maxima unrelated to model bias —
    widening sigma prevents false confidence without changing the direction.
    """
    qualifying = [
        s for s in city_skill.values()
        if (s.mae_f is not None and float(s.mae_f) > BETA_VARIANCE_MAE_MIN
            and s.bias_f is not None and abs(float(s.bias_f)) < BETA_VARIANCE_BIAS_MAX)
    ]
    return len(qualifying) >= BETA_VARIANCE_MIN_SOURCES


def _lead_sigma(days_ahead: Optional[int]) -> float:
    if days_ahead is None or days_ahead < 0:
        return 4.5
    return min(6.0, 4.0 + 0.5 * days_ahead)


def beta_blend_with_market(
    p_beta: float,
    market_prob: Optional[float],
    weight: float = BETA_MARKET_BLEND_WEIGHT,
) -> Tuple[float, Optional[dict]]:
    """Temper beta's probability toward the market price.

    Empirically beta's confidence is anti-predictive at the top end while the
    market price tracks realized outcomes far better (75¢ entries resolved ~64%
    while beta claimed 95%). Blending pulls beta toward the market and shrinks
    the anti-predictive edge by `weight`. Both probabilities are on the YES
    (P-in-bucket) scale. Returns (blended_prob, info); info is None when no
    market price is available so beta degrades to its own estimate.
    """
    if market_prob is None:
        return p_beta, None
    mp = max(0.0, min(1.0, float(market_prob)))
    w = max(0.0, min(1.0, float(weight)))
    blended = w * float(p_beta) + (1.0 - w) * mp
    info = {
        "p_beta": round(float(p_beta), 4),
        "market_prob": round(mp, 4),
        "beta_weight": round(w, 3),
        "blended": round(float(blended), 4),
    }
    return blended, info


# ── Main estimation function ──────────────────────────────────────────────────

def beta_estimate_with_breakdown(
    signals: dict,
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    days_ahead: Optional[int] = None,
    bucket_unit: str = "F",
    city_skill: Optional[dict] = None,
) -> Tuple[float, dict]:
    """Beta probability estimator. Same return interface as estimate_with_breakdown().

    city_skill: dict keyed by ModelSkill.source name ('gfs', 'ecmwf', etc.)
    containing ModelSkill rows for the current (city, days_ahead). Pass {} or
    None when no skill data is available — beta degrades gracefully to alpha-like
    behaviour via the station_bias fallback.
    """
    if city_skill is None:
        city_skill = {}

    is_low_market = signals.get("is_low_market", False)
    fc_key = "predicted_low_f" if is_low_market else "predicted_high_f"
    ensemble_key = "ensemble_lows" if is_low_market else "ensemble_highs"

    observation_skipped = days_ahead is not None and days_ahead >= 1
    is_open_ended = (bucket_min is None) != (bucket_max is None)
    station_bias = signals.get("station_bias") or {}
    sigma_lead = _lead_sigma(days_ahead)

    is_variance = _is_variance_city(city_skill) if city_skill else False

    # Pool ensemble members, applying per-source beta bias correction
    ensemble_key_pairs = (
        ("gfs_ensemble", "gfs_forecast", "gfs"),
        ("ecmwf_ensemble", "ecmwf_forecast", "ecmwf"),
    )
    ensemble_vals: list = []
    ensemble_models: list[str] = []
    for ens_key, parent_key, src_name in ensemble_key_pairs:
        fc = signals.get(ens_key) or {}
        members = fc.get(ensemble_key) or []
        if members:
            skill = city_skill.get(src_name)
            b = _beta_source_bias(skill, parent_key, station_bias)
            ensemble_vals.extend(float(v) + b for v in members)
            ensemble_models.append(ens_key)

    sigma_base, ensemble_std_f = _effective_sigma(sigma_lead, ensemble_vals)
    sigma_global = sigma_base
    if is_variance:
        sigma_global = min(BETA_SIGMA_CAP, sigma_global * BETA_SIGMA_VARIANCE_MULT)
    if is_open_ended:
        sigma_global *= 1.5

    breakdown: dict = {
        "estimator": "beta",
        "is_low_market": bool(is_low_market),
        "days_ahead": int(days_ahead) if days_ahead is not None else None,
        "sigma_used": float(sigma_global),
        "sigma_lead": float(sigma_lead),
        "ensemble_std_f": ensemble_std_f,
        "ensemble_models": ensemble_models,
        "is_open_ended": is_open_ended,
        "student_t_df": BETA_STUDENT_T_DF,
        "observation_skipped": bool(observation_skipped),
        "bucket_unit": bucket_unit,
        "is_variance_city": is_variance,
        "deterministic": [],
        "ensemble": None,
        "wunderground": None,
        "det_avg": None,
        "forecast_high_f": None,
        "ens_p": None,
        "wg_p": None,
        "blend_before_adjustments": None,
        "boundary_risk": None,
        "model_disagreement": None,
        "straddle_info": None,
        "blocked_sources": [],
        "adjustments": [],
        "final": None,
    }

    unavailable_api: set[str] = set(signals.get("_unavailable_api") or [])

    det_probs: list[float] = []
    det_vals: list[float] = []
    det_wp_sum = 0.0
    det_w_sum = 0.0
    n_global_det = 0
    missing_sources: list[str] = []
    missing_no_key: list[str] = []
    missing_conus_only: list[str] = []

    for signals_key, label, is_global, src_name in _DET_SOURCES:
        src_data = signals.get(signals_key) or {}
        val = src_data.get(fc_key)
        if val is None:
            if not is_global:
                missing_conus_only.append(label)
            elif signals_key in unavailable_api:
                missing_no_key.append(label)
            else:
                missing_sources.append(label)
            continue

        skill = city_skill.get(src_name)

        if _is_blocked(skill):
            breakdown["blocked_sources"].append({
                "source": label,
                "bias_f": float(skill.bias_f),
                "reason": f"|bias_f|={abs(skill.bias_f):.1f}°F > {BETA_BLOCK_BIAS_F}°F threshold",
            })
            if is_global:
                n_global_det += 1
            continue

        bias_correction = _beta_source_bias(skill, signals_key, station_bias)
        corrected_val = float(val) + bias_correction
        src_sigma = _beta_source_sigma(skill, sigma_global)

        p = _student_t_bucket_prob(
            corrected_val, bucket_min, bucket_max, sigma=src_sigma, unit=bucket_unit
        )
        if p is None:
            if not is_global:
                missing_conus_only.append(label)
            else:
                missing_sources.append(label)
            continue

        if is_global:
            n_global_det += 1

        w = _beta_source_weight(skill)
        det_probs.append(p)
        det_vals.append(float(corrected_val))
        det_wp_sum += w * p
        det_w_sum += w

        det_entry: dict = {
            "source": label,
            "value_f": float(corrected_val),
            "raw_value_f": float(val),
            "bias_correction_f": float(bias_correction),
            "sigma_used": float(src_sigma),
            "p_in_bucket": float(p),
            "weight": round(w, 3),
        }
        if skill is not None:
            det_entry["skill"] = {
                "samples": skill.samples,
                "hit_rate": round(float(skill.hit_rate), 3) if skill.hit_rate is not None else None,
                "mae_f": round(float(skill.mae_f), 2) if skill.mae_f is not None else None,
                "bias_f": round(float(skill.bias_f), 2) if skill.bias_f is not None else None,
            }
        breakdown["deterministic"].append(det_entry)

    det_p = (det_wp_sum / det_w_sum) if det_w_sum > 0 else None
    n_det = len(det_probs)
    breakdown["det_avg"] = float(det_p) if det_p is not None else None
    breakdown["missing_sources"] = missing_sources
    breakdown["missing_no_key"] = missing_no_key
    breakdown["missing_conus_only"] = missing_conus_only

    ens_p = _ensemble_bucket_prob(ensemble_vals, bucket_min, bucket_max, unit=bucket_unit)
    if ens_p is not None:
        hits = sum(1 for v in ensemble_vals if _bucket_contains(v, bucket_min, bucket_max, unit=bucket_unit))
        n = len(ensemble_vals)
        pooled_sorted = sorted(ensemble_vals)
        pooled_median = pooled_sorted[n // 2] if n else None
        breakdown["ensemble"] = {
            "n": n, "hits": hits,
            "median_f": round(pooled_median, 1) if pooled_median is not None else None,
            "raw_pct": round(100 * hits / n, 1) if n else None,
            "smoothed_pct": round(100 * ens_p, 1),
            "models": ensemble_models,
        }
    breakdown["ens_p"] = float(ens_p) if ens_p is not None else None

    wg_src = signals.get("wunderground_forecast") or {}
    wg_val = wg_src.get(fc_key)
    wg_skill = city_skill.get("wunderground")
    wg_corrected = (
        float(wg_val) + _beta_source_bias(wg_skill, "wunderground_forecast", station_bias)
        if wg_val is not None else None
    )
    wg_sigma = _beta_source_sigma(wg_skill, sigma_global)
    wg_p = (
        _student_t_bucket_prob(wg_corrected, bucket_min, bucket_max, sigma=wg_sigma, unit=bucket_unit)
        if wg_corrected is not None else None
    )
    if wg_val is not None:
        breakdown["wunderground"] = {
            "value_f": float(wg_corrected),
            "raw_value_f": float(wg_val),
            "p_in_bucket": float(wg_p) if wg_p is not None else None,
        }
    breakdown["wg_p"] = float(wg_p) if wg_p is not None else None

    breakdown["has_forecast_data"] = bool(
        det_p is not None or ens_p is not None or wg_p is not None
    )

    all_source_forecasts = list(det_vals)
    if wg_corrected is not None:
        all_source_forecasts.append(float(wg_corrected))

    # ── Ensemble / deterministic weight blend (same logic as alpha) ────────────
    ensemble_weight = _ENSEMBLE_WEIGHT_BASE
    det_weight = 1.0 - ensemble_weight
    if len(all_source_forecasts) >= 2:
        source_spread = max(all_source_forecasts) - min(all_source_forecasts)
        if source_spread > _SOURCE_SPREAD_THRESHOLD_F:
            excess = min(
                source_spread - _SOURCE_SPREAD_THRESHOLD_F,
                _SOURCE_SPREAD_MAX_BLEND_F - _SOURCE_SPREAD_THRESHOLD_F,
            )
            reduction = (
                excess / (_SOURCE_SPREAD_MAX_BLEND_F - _SOURCE_SPREAD_THRESHOLD_F)
            ) * (_ENSEMBLE_WEIGHT_BASE - _ENSEMBLE_WEIGHT_MIN)
            ensemble_weight = _ENSEMBLE_WEIGHT_BASE - reduction
        breakdown["model_disagreement"] = {
            "source_spread_f": round(source_spread, 2),
            "ensemble_weight_used": round(ensemble_weight, 4),
            "det_weight_used": round(1.0 - ensemble_weight, 4),
        }
        det_weight = 1.0 - ensemble_weight

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

    # ── Boundary proximity blend ───────────────────────────────────────────────
    fc_for_boundary: Optional[float] = None
    if det_vals:
        fc_for_boundary = sum(det_vals) / len(det_vals)
    elif wg_corrected is not None:
        fc_for_boundary = float(wg_corrected)
    elif ensemble_vals:
        fc_for_boundary = sum(ensemble_vals) / len(ensemble_vals)

    breakdown["forecast_high_f"] = float(fc_for_boundary) if fc_for_boundary is not None else None

    if fc_for_boundary is not None:
        f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)
        edges = [e for e in (f_lo, f_hi) if e is not None]
        if edges:
            dist = min(abs(fc_for_boundary - e) for e in edges)
            if dist < _BOUNDARY_WINDOW_F:
                blend_w = _BOUNDARY_MAX_BLEND * (1.0 - dist / _BOUNDARY_WINDOW_F)
                p_before = p
                p = (1.0 - blend_w) * p + blend_w * 0.5
                breakdown["boundary_risk"] = {
                    "avg_forecast_f": float(fc_for_boundary),
                    "blend_weight": float(blend_w),
                }
                breakdown["adjustments"].append(
                    {"name": "Boundary proximity", "delta": float(p - p_before)}
                )

    # ── Straddle detection ─────────────────────────────────────────────────────
    if all_source_forecasts and (bucket_min is not None or bucket_max is not None):
        f_lo_s, f_hi_s = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)

        def _inside(f: float) -> bool:
            if f_lo_s is not None and f < f_lo_s: return False
            if f_hi_s is not None and f >= f_hi_s: return False
            return True

        sources_inside = [f for f in all_source_forecasts if _inside(f)]
        sources_outside = [f for f in all_source_forecasts if not _inside(f)]
        straddles = bool(sources_inside and sources_outside)
        breakdown["straddle_info"] = {
            "straddles": straddles,
            "inside_sources": [round(f, 2) for f in sources_inside],
            "outside_sources": [round(f, 2) for f in sources_outside],
        }
        if straddles:
            fraction_inside = len(sources_inside) / len(all_source_forecasts)
            straddle_blend = _STRADDLE_EXTRA_BLEND * fraction_inside
            p_before = p
            p = p * (1 - straddle_blend) + 0.50 * straddle_blend
            breakdown["adjustments"].append(
                {"name": "Straddle blend", "delta": float(p - p_before)}
            )

    # ── Intraday adjustments (METAR trend, wind, PIREP, dew point, gradient) ──
    # Observational, not model-based — same as alpha.
    if not observation_skipped:
        if bucket_min is not None:
            native_floor_f = (
                bucket_min * 9.0 / 5.0 + 32.0 if bucket_unit == "C" else float(bucket_min)
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
        onshore_center = signals.get("_onshore_wind_dir")
        if ref_wind_dir is not None and ref_wind_kt > 8 and onshore_center is not None:
            diff = abs((float(ref_wind_dir) - float(onshore_center) + 180.0) % 360.0 - 180.0)
            onshore = diff <= 55
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
            pirep_p = _student_t_bucket_prob(avg_f, bucket_min, bucket_max, sigma=4.0, unit=bucket_unit)
            if pirep_p is not None:
                p = 0.95 * p + 0.05 * pirep_p
        if abs(p - p_before) > 1e-6:
            breakdown["adjustments"].append({"name": "Low-altitude PIREP", "delta": float(p - p_before)})

        p_before = p
        primary = signals.get("primary_metar") or {}
        primary_temp_f = primary.get("temperature_f")
        primary_dew_f = primary.get("dew_point_f")
        if primary_temp_f is not None and primary_dew_f is not None and bucket_requires_warmth:
            if primary_temp_f - primary_dew_f < 5.0:
                p = max(_PROB_CLIP_LO, p * 0.92)
        if abs(p - p_before) > 1e-6:
            breakdown["adjustments"].append({"name": "Dew point convergence", "delta": float(p - p_before)})

        p_before = p
        ref_temp_f = ref.get("temperature_f")
        if primary_temp_f is not None and ref_temp_f is not None and bucket_requires_warmth:
            if primary_temp_f - ref_temp_f > 8.0:
                p = max(_PROB_CLIP_LO, p * 0.91)
        if abs(p - p_before) > 1e-6:
            breakdown["adjustments"].append(
                {"name": "Station gradient (sea/lake-breeze proxy)", "delta": float(p - p_before)}
            )

    # ── Sparse-source shrinkage ────────────────────────────────────────────────
    n_global_missing = max(0, _SPARSE_SOURCE_BASELINE - n_global_det)
    if n_global_missing > 0:
        shrink = n_global_missing * _SPARSE_SOURCE_SHRINK_PER_MISSING
        p_before = p
        p = p * (1.0 - shrink) + 0.5 * shrink
        breakdown["adjustments"].append({"name": "Sparse sources", "delta": float(p - p_before)})
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
