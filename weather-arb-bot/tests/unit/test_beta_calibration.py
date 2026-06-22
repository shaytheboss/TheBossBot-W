"""Tests for the beta-estimator calibration fixes (overconfidence repair).

Root cause analyzed from live results: beta's confidence was anti-predictive at
the top end (96% claimed → 50% realized). Three isolated fixes, all beta-only:

1. Bias-correction shrinkage by sample count (noisy bias_f → applied partially).
2. Sigma widening: raised floor (4°F) + bias-estimate uncertainty (mae/sqrt(n))
   folded in via quadrature.
3. Market-price blend: pull beta toward the market, which tracks outcomes better.

None of these touch alpha (probability_estimator.py).
"""
import math
from types import SimpleNamespace

import pytest

from app.analyzers.beta_estimator import (
    BETA_BIAS_SHRINK_K,
    BETA_SIGMA_FLOOR,
    BETA_MARKET_BLEND_WEIGHT,
    _beta_source_bias,
    _beta_source_sigma,
    beta_blend_with_market,
    beta_estimate_with_breakdown,
)


def _skill(**kw):
    base = dict(bias_f=None, mae_f=None, samples=0, hits=0, hit_rate=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── Fix 1: bias shrinkage ───────────────────────────────────────────────────────

def test_bias_shrinkage_scales_with_samples():
    # bias_f=+4.0 means model runs hot → correction is negative (cool it down).
    # With K=20: n=20 → 50% applied; n=5 → 20% applied.
    s20 = _skill(bias_f=4.0, samples=20)
    s5 = _skill(bias_f=4.0, samples=5)
    b20 = _beta_source_bias(s20, "gfs_forecast", {})
    b5 = _beta_source_bias(s5, "gfs_forecast", {})
    assert b20 == pytest.approx(-4.0 * (20 / (20 + BETA_BIAS_SHRINK_K)))  # -2.0
    assert b5 == pytest.approx(-4.0 * (5 / (5 + BETA_BIAS_SHRINK_K)))     # -0.8
    # More samples → trust the correction more (larger magnitude).
    assert abs(b20) > abs(b5)


def test_bias_full_correction_only_with_many_samples():
    s = _skill(bias_f=4.0, samples=180)
    b = _beta_source_bias(s, "gfs_forecast", {})
    # Approaches -4.0 but never exceeds it.
    assert -4.0 < b < -3.5


def test_bias_zero_samples_falls_back_not_full_correction():
    # samples=0 must NOT apply the raw bias; it falls back to station/global prior.
    s = _skill(bias_f=4.0, samples=0)
    b = _beta_source_bias(s, "gfs_forecast", {})
    assert b == pytest.approx(1.5)  # global fallback default, not -4.0


# ── Fix 2: sigma widening ────────────────────────────────────────────────────────

def test_sigma_floor_is_four():
    # Accurate model, tons of samples — still cannot go below the 4°F floor.
    s = _skill(mae_f=1.0, samples=200)
    sig = _beta_source_sigma(s, sigma_global=4.0)
    assert sig >= BETA_SIGMA_FLOOR == 4.0


def test_thin_samples_widen_sigma():
    # Same MAE, but few resolutions → wider sigma (bias-estimate uncertainty).
    thin = _skill(mae_f=3.0, samples=4)
    thick = _skill(mae_f=3.0, samples=200)
    sig_thin = _beta_source_sigma(thin, sigma_global=4.0)
    sig_thick = _beta_source_sigma(thick, sigma_global=4.0)
    assert sig_thin > sig_thick
    # quadrature check: base=max(4.0, 3.6)=4.0, unc=3.0/sqrt(4)=1.5
    expected_thin = math.sqrt(4.0 ** 2 + 1.5 ** 2)
    assert sig_thin == pytest.approx(expected_thin)


def test_sigma_respects_cap():
    s = _skill(mae_f=20.0, samples=2)
    assert _beta_source_sigma(s, sigma_global=4.0) <= 7.0


def test_no_skill_returns_global_sigma():
    assert _beta_source_sigma(None, sigma_global=5.5) == 5.5


# ── Fix 3: market blend ──────────────────────────────────────────────────────────

def test_market_blend_pulls_toward_market():
    blended, info = beta_blend_with_market(0.95, 0.75)
    assert blended == pytest.approx(0.6 * 0.95 + 0.4 * 0.75)  # 0.87
    assert info is not None
    assert info["market_prob"] == 0.75
    assert info["beta_weight"] == pytest.approx(BETA_MARKET_BLEND_WEIGHT)


def test_market_blend_no_price_is_passthrough():
    blended, info = beta_blend_with_market(0.95, None)
    assert blended == 0.95
    assert info is None


def test_market_blend_shrinks_edge():
    # beta 0.95 vs market 0.75 → raw edge 0.20; blended edge must be ~0.6×.
    blended, _ = beta_blend_with_market(0.95, 0.75)
    raw_edge = 0.95 - 0.75
    blended_edge = blended - 0.75
    assert blended_edge == pytest.approx(BETA_MARKET_BLEND_WEIGHT * raw_edge)


def test_market_blend_clamps_market_prob():
    blended, info = beta_blend_with_market(0.90, 1.7)
    assert info["market_prob"] == 1.0
    assert blended == pytest.approx(0.6 * 0.90 + 0.4 * 1.0)


# ── Integration: floor wired through the full estimator ──────────────────────────

def test_estimator_uses_widened_sigma():
    signals = {
        "gfs_forecast": {"predicted_high_f": 88.0},
        "ecmwf_forecast": {"predicted_high_f": 88.5},
    }
    city_skill = {
        "gfs": _skill(mae_f=1.0, samples=8, bias_f=0.0, hits=7, hit_rate=0.875),
        "ecmwf": _skill(mae_f=1.2, samples=8, bias_f=0.0, hits=7, hit_rate=0.875),
    }
    final, bd = beta_estimate_with_breakdown(
        signals, 92, 93, days_ahead=0, bucket_unit="F", city_skill=city_skill,
    )
    assert bd["estimator"] == "beta"
    assert bd["sigma_used"] >= BETA_SIGMA_FLOOR
    # every per-source sigma must respect the 4°F floor
    for entry in bd["deterministic"]:
        assert entry["sigma_used"] >= BETA_SIGMA_FLOOR
    assert 0.0 <= final <= 1.0


def test_estimator_far_forecast_not_maximally_confident():
    """Forecast 88°F vs 92-93°F bucket. The old tight sigma made this a ~93%+ NO
    automatic buy that lost. With the 4°F floor the NO certainty must back off."""
    signals = {"gfs_forecast": {"predicted_high_f": 88.0}}
    city_skill = {"gfs": _skill(mae_f=1.0, samples=8, bias_f=0.0, hits=7, hit_rate=0.875)}
    final, _ = beta_estimate_with_breakdown(
        signals, 92, 93, days_ahead=0, bucket_unit="F", city_skill=city_skill,
    )
    no_certainty = 1.0 - final
    assert no_certainty <= 0.905, f"NO certainty {no_certainty:.3f} still overconfident"


# ── Resolution alert labeling (alpha vs beta) ────────────────────────────────────

def test_resolution_estimator_tag():
    from app.workers.jobs import _estimator_tag
    assert _estimator_tag(SimpleNamespace(estimator="beta")) == "[β]"
    assert _estimator_tag(SimpleNamespace(estimator="alpha")) == "[α]"
    # legacy rows: NULL / missing estimator → treated as alpha
    assert _estimator_tag(SimpleNamespace(estimator=None)) == "[α]"
    assert _estimator_tag(SimpleNamespace()) == "[α]"
