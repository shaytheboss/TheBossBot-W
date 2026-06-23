"""Tests for the unconfirmed-lock sigma and stat-cap fix.

Root cause (Seoul/HK/Dallas incidents): when METAR exceeds the bucket ceiling
but WU hasn't confirmed yet (yes_impossible_unconfirmed), σ=0.3°F manufactured
99% NO confidence.  WU then resolves at a lower value → loss.

Two targeted fixes (intraday-only, no alpha/beta contact):

1. Sigma override: for yes_impossible_unconfirmed, σ is raised to at least
   unconfirmed_lock_sigma_f (2.0°F) to reflect the typical 1-3°F METAR-WU
   station divergence.  With σ=2.0, a 2°F METAR excess → 84% NO (below the
   94% buy threshold) instead of the old 99%.

2. Stat cap extended: the 96%/4% statistical ceiling now covers
   yes_impossible_unconfirmed cases even after peak, in addition to pre-peak
   non-locks.  Normal post-peak (lock_state=None, peak confirmed) remains
   exempt — that case IS genuinely high-confidence.
"""
import math

import pytest

from app.intraday.estimator import DEFAULT_PARAMS, PROB_HI, PROB_LO, estimate_intraday


# ── Helper: build a post-peak unconfirmed scenario ──────────────────────────

def _unconfirmed(metar_excess_f: float, bucket_width_f: float = 2.0) -> dict:
    """Post-peak: METAR is metar_excess_f above the bucket ceiling.  WU not confirmed."""
    # bucket [84, 86)°F (±0.5 half-bin → [83.5, 86.5)); METAR at ceiling + excess.
    ceiling = 86.5  # f_hi as seen by bucket_probability
    return dict(
        running_max_f=ceiling + metar_excess_f,
        current_temp_f=ceiling + metar_excess_f - 3.0,  # clearly cooling
        minutes_since_max=120.0,
        forecast_high_f=ceiling,
        local_hour=16.5,            # past peak_confirm_hour=15.5
        bucket_min=84, bucket_max=86, bucket_unit="F",
        wu_confirmed=False,
    )


# ── Fix 1: sigma override ────────────────────────────────────────────────────

def test_unconfirmed_lock_sigma_raised_to_floor():
    """yes_impossible_unconfirmed must use ≥ unconfirmed_lock_sigma_f."""
    _, bd = estimate_intraday(**_unconfirmed(metar_excess_f=1.5))
    assert bd["lock_state"] == "yes_impossible_unconfirmed"
    assert bd["unconfirmed_sigma_applied"] is True
    assert bd["sigma_used"] >= DEFAULT_PARAMS.unconfirmed_lock_sigma_f


def test_unconfirmed_lock_sigma_not_applied_to_wide_pre_peak_sigma():
    """If the schedule + quadrature already exceeds 2°F, no override fires."""
    # Pre-peak at 10:00 — schedule σ=2.2, forecast term ≈2.5, combined >3°F.
    _, bd = estimate_intraday(
        running_max_f=87.0, current_temp_f=86.0, minutes_since_max=5.0,
        forecast_high_f=87.5, local_hour=10.0,
        bucket_min=84, bucket_max=86, bucket_unit="F",
        wu_confirmed=False,
    )
    # State: running_max=87 ≥ f_hi=86.5 → yes_impossible_unconfirmed
    assert bd["lock_state"] == "yes_impossible_unconfirmed"
    # pre-peak schedule sigma is already wide — override does not shrink it
    assert bd["unconfirmed_sigma_applied"] is False
    assert bd["sigma_used"] >= DEFAULT_PARAMS.unconfirmed_lock_sigma_f


def test_confirmed_lock_sigma_not_overridden():
    """yes_impossible (WU confirmed) must NOT get the unconfirmed sigma override."""
    _, bd = estimate_intraday(
        running_max_f=90.0, current_temp_f=87.0, minutes_since_max=120.0,
        forecast_high_f=89.0, local_hour=16.5,
        bucket_min=84, bucket_max=86, bucket_unit="F",
        wu_confirmed=True,
    )
    assert bd["lock_state"] == "yes_impossible"
    assert bd["unconfirmed_sigma_applied"] is False
    # Hard lock returns PROB_LO, not the statistical path
    _, bd2 = estimate_intraday(
        running_max_f=90.0, current_temp_f=87.0, minutes_since_max=120.0,
        forecast_high_f=89.0, local_hour=16.5,
        bucket_min=84, bucket_max=86, bucket_unit="F",
        wu_confirmed=True,
    )
    p, _ = estimate_intraday(
        running_max_f=90.0, current_temp_f=87.0, minutes_since_max=120.0,
        forecast_high_f=89.0, local_hour=16.5,
        bucket_min=84, bucket_max=86, bucket_unit="F",
        wu_confirmed=True,
    )
    assert p == PROB_LO  # mathematical lock stays pinned


def test_no_lock_state_never_gets_override():
    """Normal post-peak with running max inside bucket: no override, no change."""
    p, bd = estimate_intraday(
        running_max_f=78.8, current_temp_f=76.0, minutes_since_max=150.0,
        forecast_high_f=78.0, local_hour=16.5,
        bucket_min=78, bucket_max=79, bucket_unit="F",
    )
    assert bd["lock_state"] is None
    assert bd["unconfirmed_sigma_applied"] is False
    # sigma stays at the post_peak_sigma floor (0.3), probability stays high
    assert bd["sigma_used"] == pytest.approx(DEFAULT_PARAMS.post_peak_sigma, abs=1e-9)
    assert p > 0.96  # genuinely high-confidence — no cap


# ── Fix 2: stat cap extended to unconfirmed locks ───────────────────────────

def test_stat_cap_fires_for_unconfirmed_post_peak_large_excess():
    """Large METAR excess pushes p to PROB_LO; stat cap then raises NO certainty
    ceiling from 98.5% (old 99 conf) to 96% (96 conf)."""
    p, bd = estimate_intraday(**_unconfirmed(metar_excess_f=5.0))
    assert bd["peak_passed"] is True
    assert bd["lock_state"] == "yes_impossible_unconfirmed"
    assert bd["stat_cap_applied"] is True
    # YES probability floored at stat_prob_lo; NO certainty capped at stat_prob_hi
    assert p == pytest.approx(DEFAULT_PARAMS.stat_prob_lo, abs=1e-9)
    assert (1.0 - p) <= DEFAULT_PARAMS.stat_prob_hi + 1e-9


def test_stat_cap_does_NOT_fire_for_normal_post_peak():
    """The exempt case (lock_state=None, post-peak) must remain uncapped."""
    p, bd = estimate_intraday(
        running_max_f=78.8, current_temp_f=76.0, minutes_since_max=150.0,
        forecast_high_f=78.0, local_hour=16.5,
        bucket_min=78, bucket_max=79, bucket_unit="F",
    )
    assert bd["peak_passed"] is True
    assert bd["stat_cap_applied"] is False
    assert p > DEFAULT_PARAMS.stat_prob_hi  # above 0.96 — not capped


def test_hard_lock_not_capped_by_stat_cap():
    """yes_impossible / yes_locked must always return PROB_LO / PROB_HI."""
    p_no, bd_no = estimate_intraday(
        running_max_f=90.0, current_temp_f=87.0, minutes_since_max=120.0,
        forecast_high_f=89.0, local_hour=16.5,
        bucket_min=84, bucket_max=86, bucket_unit="F",
        wu_confirmed=True,  # hard lock
    )
    assert bd_no["lock_state"] == "yes_impossible"
    assert p_no == PROB_LO
    assert bd_no["stat_cap_applied"] is False

    p_yes, bd_yes = estimate_intraday(
        running_max_f=86.4, current_temp_f=86.0, minutes_since_max=20.0,
        forecast_high_f=87.0, local_hour=12.0,
        bucket_min=86, bucket_max=None, bucket_unit="F",
    )
    assert bd_yes["lock_state"] == "yes_locked"
    assert p_yes == PROB_HI
    assert bd_yes["stat_cap_applied"] is False


# ── Combined effect: moderate METAR excess avoids the buy threshold ──────────

def test_moderate_unconfirmed_excess_below_buy_threshold():
    """The key fix: METAR 2°F above ceiling, post-peak, unconfirmed.

    Old behavior: σ=0.3 → p≈0% → PROB_LO → NO certainty 98.5% → auto-buy.
    New behavior: σ=2.0 → p≈16% → NO certainty 84% → no alert, no buy.
    """
    p, bd = estimate_intraday(**_unconfirmed(metar_excess_f=2.0))
    no_certainty = 1.0 - p
    assert no_certainty < 0.94, (
        f"NO certainty {no_certainty:.3f} still above buy threshold — "
        f"Seoul/HK/Dallas failure mode is back (sigma={bd['sigma_used']:.2f}°F)"
    )
    assert bd["unconfirmed_sigma_applied"] is True
    assert bd["sigma_used"] >= DEFAULT_PARAMS.unconfirmed_lock_sigma_f


def test_small_unconfirmed_excess_below_alert_threshold():
    """1°F METAR excess: WU-METAR divergence makes this unreliable. No alert."""
    p, bd = estimate_intraday(**_unconfirmed(metar_excess_f=1.0))
    no_certainty = 1.0 - p
    # With σ=2.0: z = -1/2 = -0.5 → p(YES) ≈ 30.9% → NO certainty ≈ 69%.
    # Well below the 90% alert threshold — the trade is silently skipped.
    assert no_certainty < 0.90, (
        f"NO certainty {no_certainty:.3f} ≥ alert threshold — "
        f"a 1°F METAR excess is too unreliable to alert"
    )


def test_large_unconfirmed_excess_capped_at_96_not_99():
    """5°F METAR excess: still NOT 99 conf after the fix."""
    p, bd = estimate_intraday(**_unconfirmed(metar_excess_f=5.0))
    confidence_score = int(round((1.0 - p) * 100))
    # Old: confidence_score = 99; New: confidence_score = 96
    assert confidence_score <= 96, (
        f"Conf {confidence_score} still shows 99-style overconfidence for "
        f"unconfirmed 5°F excess"
    )


# ── Regression: pre-peak unconfirmed still works as before ──────────────────

def test_pre_peak_unconfirmed_still_statistical():
    """Pre-peak yes_impossible_unconfirmed is already capped by the existing
    stat-cap rule — ensure the new changes don't break this."""
    p, bd = estimate_intraday(
        running_max_f=93.0, current_temp_f=91.0, minutes_since_max=20.0,
        forecast_high_f=93.5, local_hour=13.0,
        bucket_min=90, bucket_max=92, bucket_unit="F",
        wu_confirmed=False,
    )
    assert bd["lock_state"] == "yes_impossible_unconfirmed"
    assert p > PROB_LO  # statistical, not pinned at hard-lock floor
