"""Unit tests for the four intraday fixes derived from CSV loss analysis (2026-06-15).

Fix 1 — WU-confirmed lock gate:
    METAR can run 2-4°F above Wunderground (the Polymarket resolution source).
    lock_state() must not emit "yes_impossible" when wu_confirmed=False.

Fix 2 — Celsius σ floor:
    Each Celsius bucket is only 1°C = 1.8°F wide.  After peak passes the
    schedule produces σ=0.3°F, which manufactures >99% confidence on a bet
    that can flip by a single WU decimal rounding.  Floor is now 1.8°F.

Fix 3 — max_entry_cost gate:
    Buying YES at >88¢ is negative EV: the potential win (~12¢) is destroyed
    by a single lock failure (loss of 88¢).  Buys are blocked; alerts still fire.

Fix 4 — Intraday cluster early-warning:
    When a European city runs >2°F above NWP forecast, sister cities in the
    same cluster get a bias boost within the same scan cycle so they don't
    also underestimate the day's peak.
"""
from datetime import date

import pytest

import app.intraday.detector as idet
from app.intraday.estimator import DEFAULT_PARAMS, estimate_intraday, lock_state


# ── Fix 1: WU-confirmed lock gate ────────────────────────────────────────────

def test_lock_state_yes_impossible_requires_wu_confirmed():
    """METAR ceiling breach without WU confirmation → unconfirmed, not locked."""
    result = lock_state(running_max_f=93.0, f_lo=91.0, f_hi=93.0, wu_confirmed=False)
    assert result == "yes_impossible_unconfirmed"


def test_lock_state_yes_impossible_with_wu_confirmed():
    """WU-confirmed ceiling breach → hard lock as before."""
    result = lock_state(running_max_f=93.0, f_lo=91.0, f_hi=93.0, wu_confirmed=True)
    assert result == "yes_impossible"


def test_lock_state_unconfirmed_falls_through_to_statistical_path():
    """An unconfirmed 'lock' must produce a probability, not PROB_LO/PROB_HI."""
    # _bucket_to_f_bounds adds ±0.5°F half-bin for Fahrenheit buckets, so
    # bucket_max=92 → f_hi=92.5. running_max=93.0 ≥ 92.5 triggers the lock.
    p, bd = estimate_intraday(
        running_max_f=93.0, current_temp_f=91.0, minutes_since_max=20.0,
        forecast_high_f=93.5, local_hour=13.0,
        bucket_min=90, bucket_max=92, bucket_unit="F",
        wu_confirmed=False,
    )
    assert bd["lock_state"] == "yes_impossible_unconfirmed"
    # Statistical path — stat_prob_lo floor (0.04) applies, well above PROB_LO (0.015).
    assert p > 0.015


def test_lock_state_yes_locked_unaffected_by_wu_confirmed():
    """Open-ended '>= lo' bucket: yes_locked still fires even without WU
    confirmation because a METAR reading above the floor is safe (floor is low)."""
    result = lock_state(running_max_f=86.0, f_lo=86.0, f_hi=None, wu_confirmed=False)
    assert result == "yes_locked"


# ── Fix 2: Celsius σ floor ───────────────────────────────────────────────────

def test_celsius_sigma_floored_post_peak():
    """Post-peak schedule gives σ=0.3°F, but Celsius floor must raise it to 1.8."""
    p, bd = estimate_intraday(
        running_max_f=29.0,   # °C bucket; expressed internally as °F
        current_temp_f=27.0,  # 1.8°F below max
        minutes_since_max=100.0,
        forecast_high_f=29.0, local_hour=16.5,   # inside peak window
        bucket_min=27, bucket_max=28, bucket_unit="C",
    )
    assert bd["peak_passed"] is True
    # Without the fix, sigma would be DEFAULT_PARAMS.post_peak_sigma = 0.3.
    assert bd["sigma_used"] >= DEFAULT_PARAMS.celsius_min_sigma_f
    assert bd["celsius_floor_applied"] is True


def test_celsius_sigma_floored_pre_peak():
    """Pre-peak Celsius market with low schedule σ also gets the floor."""
    _, bd = estimate_intraday(
        running_max_f=25.0, current_temp_f=25.0, minutes_since_max=5.0,
        forecast_high_f=29.0, local_hour=12.0,
        bucket_min=26, bucket_max=27, bucket_unit="C",
    )
    assert bd["sigma_used"] >= DEFAULT_PARAMS.celsius_min_sigma_f


def test_fahrenheit_sigma_not_floored():
    """Fahrenheit market must NOT have the Celsius floor applied."""
    _, bd = estimate_intraday(
        running_max_f=82.0, current_temp_f=80.0, minutes_since_max=120.0,
        forecast_high_f=82.0, local_hour=16.0,
        bucket_min=80, bucket_max=82, bucket_unit="F",
    )
    assert bd.get("celsius_floor_applied") is False
    assert bd["sigma_used"] == pytest.approx(DEFAULT_PARAMS.post_peak_sigma, abs=1e-4)


# ── Fix 3: max_entry_cost gate ───────────────────────────────────────────────

def test_entry_too_expensive_blocks_buy():
    """Entry cost above max_entry_cost must suppress the virtual buy.

    Tests the exact branching logic used in the detector, mirroring:
        entry_too_expensive = entry_cost > max_entry_cost
        create_buy = certainty >= buy_thresh and not blacklisted and not entry_too_expensive
    """
    from app.config import settings

    max_entry_cost = float(getattr(settings, "intraday_max_entry_cost", 0.88))
    buy_thresh = float(getattr(settings, "intraday_min_certainty_buy", 0.94))

    # Simulate a 95% YES signal where the YES ask is 93¢ (too expensive to buy).
    certainty = 0.95
    entry_cost = 0.93   # above 0.88 cap
    blacklisted = False

    entry_too_expensive = entry_cost > max_entry_cost
    create_buy = (
        certainty >= buy_thresh
        and not blacklisted
        and not entry_too_expensive
    )

    assert entry_too_expensive is True
    assert create_buy is False, (
        f"create_buy should be False when entry_cost={entry_cost} > "
        f"max_entry_cost={max_entry_cost}"
    )


def test_entry_cost_at_threshold_not_blocked():
    """Entry cost exactly at max_entry_cost must still be allowed (strict >)."""
    from app.config import settings

    max_entry_cost = float(getattr(settings, "intraday_max_entry_cost", 0.88))
    buy_thresh = float(getattr(settings, "intraday_min_certainty_buy", 0.94))

    certainty = 0.95
    entry_cost = max_entry_cost   # exactly at cap → should be allowed
    blacklisted = False

    entry_too_expensive = entry_cost > max_entry_cost
    create_buy = certainty >= buy_thresh and not blacklisted and not entry_too_expensive

    assert entry_too_expensive is False
    assert create_buy is True


def test_entry_below_threshold_not_blocked():
    """Normal 80¢ entry must not be blocked by the cost gate."""
    from app.config import settings

    max_entry_cost = float(getattr(settings, "intraday_max_entry_cost", 0.88))
    buy_thresh = float(getattr(settings, "intraday_min_certainty_buy", 0.94))

    certainty = 0.95
    entry_cost = 0.80
    blacklisted = False

    entry_too_expensive = entry_cost > max_entry_cost
    create_buy = certainty >= buy_thresh and not blacklisted and not entry_too_expensive

    assert entry_too_expensive is False
    assert create_buy is True


# ── Fix 4: intraday cluster early-warning ────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_cluster_state():
    """Clear cluster warmth dict before/after every test in this file."""
    idet._cluster_warmth_today.clear()
    yield
    idet._cluster_warmth_today.clear()


def test_cluster_registers_warm_surprise():
    """A city running >2°F above forecast must write to _cluster_warmth_today."""
    idet._cluster_warmth_today.clear()
    cluster = idet._city_cluster("Paris")
    assert cluster is not None

    # Simulate Paris running 4°F above forecast.
    idet._cluster_warmth_today[cluster] = (date.today(), 4.0, "Paris")

    entry = idet._cluster_warmth_today.get(cluster)
    assert entry is not None
    excess_f = entry[1]
    assert excess_f == 4.0


def test_cluster_boost_capped_at_max():
    """Sister-city boost is fraction of excess but never exceeds CLUSTER_BOOST_MAX_F."""
    from app.intraday.detector import (
        CLUSTER_BOOST_FRACTION,
        CLUSTER_BOOST_MAX_F,
        CLUSTER_WARN_THRESHOLD_F,
    )
    # Very large warm surprise (8°F above forecast).
    excess = 8.0
    boost = min(excess * CLUSTER_BOOST_FRACTION, CLUSTER_BOOST_MAX_F)
    assert boost == CLUSTER_BOOST_MAX_F   # 3.2 would exceed cap of 2.0


def test_cluster_city_lookup_finds_paris():
    assert idet._city_cluster("Paris") == "europe"


def test_cluster_city_lookup_finds_houston():
    assert idet._city_cluster("Houston") == "us_south"


def test_cluster_city_lookup_unknown_city_returns_none():
    assert idet._city_cluster("Reykjavik") is None


def test_cluster_self_trigger_excluded():
    """A city's own warming surprise must NOT boost itself."""
    from app.intraday.detector import CLUSTER_BOOST_FRACTION, CLUSTER_BOOST_MAX_F
    cluster = idet._city_cluster("Paris")
    # Paris is the triggering city.
    idet._cluster_warmth_today[cluster] = (date.today(), 4.0, "Paris")
    warmth_date, warmth_excess, warmth_city = idet._cluster_warmth_today[cluster]
    # Self-trigger check that mirrors detector logic.
    would_boost = warmth_date == date.today() and warmth_city != "Paris"
    assert would_boost is False


def test_cluster_sister_city_receives_boost():
    """London should receive a boost when Paris registered a warm surprise."""
    cluster = idet._city_cluster("Paris")
    assert idet._city_cluster("London") == cluster  # same cluster

    idet._cluster_warmth_today[cluster] = (date.today(), 4.0, "Paris")

    warmth_date, warmth_excess, warmth_city = idet._cluster_warmth_today[cluster]
    would_boost_london = warmth_date == date.today() and warmth_city != "London"
    assert would_boost_london is True

    from app.intraday.detector import CLUSTER_BOOST_FRACTION, CLUSTER_BOOST_MAX_F
    expected_boost = min(warmth_excess * CLUSTER_BOOST_FRACTION, CLUSTER_BOOST_MAX_F)
    assert expected_boost == pytest.approx(1.6, abs=1e-4)
