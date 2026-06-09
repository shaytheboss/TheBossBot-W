"""Unit tests for the dashboard-accuracy bug fixes and methodology upgrades:

- normalization runs only over a COMPLETE bucket set and re-clips afterwards
- per-source bias lookup with overall/default fallback
- ensemble-spread-blended sigma (with clamps and minimum member count)
- circular onshore-wind matching
- per-city model weights from win/loss tallies
- discovery date parsing across a year rollover
"""
from datetime import date, timedelta

from app.analyzers.opportunity_detector import normalization_scale
from app.analyzers.probability_estimator import (
    _PROB_CLIP_HI,
    _clip,
    _effective_sigma,
    _is_onshore,
    _source_bias,
)
from app.analyzers.model_weights import weights_from_tallies
from app.workers.jobs import _parse_date


# --- normalization -----------------------------------------------------------

def test_normalization_skipped_when_buckets_missing():
    # 3 of 8 buckets produced data → rescaling would inflate them. Skip.
    assert normalization_scale([0.2, 0.2, 0.1], n_market_outcomes=8) is None


def test_normalization_skipped_on_tiny_total():
    assert normalization_scale([0.02, 0.03], n_market_outcomes=2) is None


def test_normalization_scale_full_set():
    scale = normalization_scale([0.4, 0.4], n_market_outcomes=2)
    assert scale is not None
    assert abs(scale - 1.25) < 1e-9


def test_clip_caps_normalized_probability():
    # 0.92 raw scaled by 1.1 would be 1.012 — the cap must hold post-scaling.
    assert _clip(0.92 * 1.1) == _PROB_CLIP_HI


# --- per-source bias ---------------------------------------------------------

def test_source_bias_prefers_per_source():
    sb = {"bias_f": 2.0, "per_source": {"gfs": -0.5, "wunderground": 0.1}}
    assert _source_bias(sb, "gfs_forecast") == -0.5
    assert _source_bias(sb, "wunderground_forecast") == 0.1
    # No per-source entry → overall bias.
    assert _source_bias(sb, "ecmwf_forecast") == 2.0


def test_source_bias_defaults():
    assert _source_bias({}, "gfs_forecast") == 1.5
    assert _source_bias(None, "gfs_forecast") == 1.5


# --- ensemble-spread sigma ----------------------------------------------------

def test_sigma_unchanged_with_few_members():
    sigma, std = _effective_sigma(4.0, [70.0] * 5)
    assert sigma == 4.0
    assert std is None


def test_tight_ensemble_narrows_sigma():
    vals = [70.0 + 0.1 * i for i in range(30)]   # std ≈ 0.87
    sigma, std = _effective_sigma(4.0, vals)
    assert std is not None
    assert sigma < 4.0
    assert sigma >= 2.0   # lower clamp


def test_wild_ensemble_widens_sigma_with_cap():
    vals = [60.0, 85.0] * 15                      # std ≈ 12.7
    sigma, _std = _effective_sigma(4.0, vals)
    assert sigma > 4.0
    assert sigma <= 7.0   # upper clamp


# --- onshore matching ---------------------------------------------------------

def test_onshore_circular_window():
    assert _is_onshore(310, 305) is True
    assert _is_onshore(250, 305) is True      # 55° away — inclusive edge
    assert _is_onshore(180, 305) is False
    # Wrap-around: onshore bearing near north.
    assert _is_onshore(350, 10) is True
    assert _is_onshore(30, 10) is True
    assert _is_onshore(180, 10) is False


# --- model weights --------------------------------------------------------------

def test_weights_need_min_samples():
    w = weights_from_tallies({"gfs_forecast": (3, 4)})
    assert w == {}   # below MIN_SAMPLES → neutral (omitted)


def test_weights_reward_accuracy():
    w = weights_from_tallies({
        "gfs_forecast": (9, 10),     # strong
        "icon_forecast": (2, 10),    # weak
    })
    assert w["gfs_forecast"] > 1.2
    assert w["icon_forecast"] < 0.9
    # Bounds: weight = 0.5 + smoothed hit-rate ∈ (0.5, 1.5).
    assert 0.5 < w["icon_forecast"] < w["gfs_forecast"] < 1.5


# --- discovery date parsing -----------------------------------------------------

def test_parse_date_year_rollover():
    # A "january-2" slug parsed in late December must land in NEXT January,
    # not 11+ months in the past.
    today = date.today()
    jan2 = _parse_date("highest-temperature-in-nyc-on-january-2")
    assert jan2 is not None
    expected_year = today.year + 1 if date(today.year, 1, 2) < today - timedelta(days=180) else today.year
    assert jan2 == date(expected_year, 1, 2)


def test_parse_date_explicit_year_untouched():
    d = _parse_date("highest-temperature-in-nyc-on-january-2-2024")
    assert d == date(2024, 1, 2)
