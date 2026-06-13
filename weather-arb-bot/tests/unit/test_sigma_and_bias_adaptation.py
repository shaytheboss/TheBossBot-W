"""Tests for the two loss-prevention upgrades:

1. Widened forecast sigma (summer daily-max errors are 3-5°F, not 2.5°F) and a
   raised ensemble-blend floor so a tight ensemble can't manufacture confidence
   below the realized error distribution.

2. Recency-weighted (EWMA) bias so a heat wave / cold snap is reflected in the
   warm-bias correction within days instead of being diluted across 14 days.
"""
from datetime import date, timedelta

import pytest

from app.analyzers.probability_estimator import (
    _SIGMA_BLEND_MIN_F,
    _effective_sigma,
    _student_t_bucket_prob,
    forecast_sigma_for_lead,
)
from app.analyzers.bias_estimator import (
    _recency_weighted_mean,
    _recent_window_mean,
    _regime_shift_note,
    BIAS_RECENCY_HALF_LIFE_DAYS,
)


# ── Fix: widened sigma ─────────────────────────────────────────────────────────

def test_same_day_sigma_widened():
    # 2.5°F implied ~93% confidence on a 4°F-off forecast — the overconfidence
    # that caused the Denver/Paris losses. Floor is now 4.0°F.
    assert forecast_sigma_for_lead(0) == 4.0


def test_sigma_grows_with_lead_and_caps():
    assert forecast_sigma_for_lead(1) == 4.5
    assert forecast_sigma_for_lead(2) == 5.0
    assert forecast_sigma_for_lead(3) == 5.5
    # capped at 6.0
    assert forecast_sigma_for_lead(10) == 6.0


def test_sigma_handles_unknown_lead():
    assert forecast_sigma_for_lead(None) == 4.5
    assert forecast_sigma_for_lead(-1) == 4.5


def test_denver_scenario_no_longer_overconfident():
    """Forecast 88°F, bucket 92-93°F. At the old σ=2.5°F this was 93% NO (an
    automatic buy that lost). At σ=4.0°F it must drop to at most ~90% so it no
    longer clears the buy threshold comfortably."""
    sigma = forecast_sigma_for_lead(0)
    p_in = _student_t_bucket_prob(88.0, 92, 93, sigma=sigma, unit="F")
    no_certainty = 1.0 - p_in
    assert no_certainty <= 0.905, f"NO certainty {no_certainty:.3f} still overconfident"
    # sanity: at the OLD sigma it WAS overconfident
    p_in_old = _student_t_bucket_prob(88.0, 92, 93, sigma=2.5, unit="F")
    assert (1.0 - p_in_old) > 0.92


def test_tight_ensemble_cannot_collapse_below_floor():
    """A near-degenerate ensemble (all members agree) used to drag the blended
    sigma below 2.0°F. The floor is now 3.0°F — a quiet day cannot manufacture
    false confidence."""
    tight = [88.0 + 0.1 * i for i in range(30)]  # std ≈ 0.87
    sigma, ens_std = _effective_sigma(4.0, tight)
    assert sigma >= _SIGMA_BLEND_MIN_F
    assert sigma == 3.0  # clamped to the floor
    assert _SIGMA_BLEND_MIN_F == 3.0


def test_wild_ensemble_still_capped_at_max():
    vals = [60.0, 85.0] * 15  # std ≈ 12.7
    sigma, _ = _effective_sigma(4.0, vals)
    assert sigma <= 7.0


# ── Fix: recency-weighted (EWMA) bias ──────────────────────────────────────────

def _dated(values_oldest_first):
    """Build (date, value) pairs ending today, one per day."""
    today = date.today()
    n = len(values_oldest_first)
    return [
        (today - timedelta(days=n - 1 - i), v)
        for i, v in enumerate(values_oldest_first)
    ]


def test_ewma_leans_toward_recent_warming():
    # 9 cool days (+1°F) then 5 hot days (+5°F): a heat wave starting.
    dated = _dated([1.0] * 9 + [5.0] * 5)
    flat = sum(v for _, v in dated) / len(dated)
    ewma = _recency_weighted_mean(dated)
    assert ewma > flat, "EWMA must lean toward the recent (hotter) days"
    # And it must stay within the data range — never overshoot.
    assert 1.0 <= ewma <= 5.0


def test_ewma_leans_toward_recent_cooling():
    dated = _dated([5.0] * 9 + [1.0] * 5)
    flat = sum(v for _, v in dated) / len(dated)
    ewma = _recency_weighted_mean(dated)
    assert ewma < flat, "EWMA must lean toward the recent (cooler) days"
    assert 1.0 <= ewma <= 5.0


def test_ewma_equals_mean_when_stable():
    dated = _dated([3.0] * 12)
    assert abs(_recency_weighted_mean(dated) - 3.0) < 1e-9


def test_ewma_bounded_and_empty_safe():
    assert _recency_weighted_mean([]) == 0.0
    dated = _dated([2.0, 4.0, 6.0])
    out = _recency_weighted_mean(dated)
    assert 2.0 <= out <= 6.0


def test_half_life_weighting_is_correct():
    # Two samples exactly one half-life apart: the newer should weigh 2× the
    # older, so the mean sits 1/3 of the way from the newer toward the older.
    today = date.today()
    older = today - timedelta(days=int(BIAS_RECENCY_HALF_LIFE_DAYS))
    dated = [(older, 0.0), (today, 3.0)]
    out = _recency_weighted_mean(dated)
    # weights: today=1.0, older=0.5 → (1.0*3 + 0.5*0)/1.5 = 2.0
    assert abs(out - 2.0) < 1e-6


# ── Regime-shift note ──────────────────────────────────────────────────────────

def test_regime_shift_note_flags_warming():
    dated = _dated([1.0] * 9 + [5.0] * 5)
    flat = sum(v for _, v in dated) / len(dated)
    note = _regime_shift_note(dated, flat)
    assert "warming regime" in note


def test_regime_shift_note_silent_when_stable():
    dated = _dated([3.0] * 12)
    assert _regime_shift_note(dated, 3.0) == ""


def test_recent_window_mean():
    dated = _dated([1.0] * 9 + [5.0] * 5)
    assert _recent_window_mean(dated, 5) == 5.0
    assert _recent_window_mean([], 5) is None
