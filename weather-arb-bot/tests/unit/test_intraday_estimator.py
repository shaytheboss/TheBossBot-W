"""Unit tests for the intraday probability model (app/intraday/estimator.py).

The model: final daily max = max(M, X), X ~ N(mu, sigma_h) where M is the
monotonic running METAR max. Locks, the gain-weight decay, the sigma
schedule and the truncated-normal bucket math are all covered here.
"""
from app.intraday.estimator import (
    DEFAULT_PARAMS,
    IntradayParams,
    PROB_HI,
    PROB_LO,
    bucket_probability,
    estimate_intraday,
    expected_final_max,
    gain_weight,
    hours_to_peak_end,
    intraday_sigma,
    is_peak_passed,
    lock_state,
)


# ── locks (the monotonic-max rules) ──────────────────────────────────────────

def test_lock_bucket_below_running_max_is_dead():
    # bucket 78-79°F (bounds 77.5–79.5) with running max already 82 → YES impossible
    assert lock_state(82.0, 77.5, 79.5) == "yes_impossible"


def test_lock_open_ended_floor_touched_is_won():
    # ">= 86°F" (lo=85.5, hi=None) with running max 86.2 → YES locked
    assert lock_state(86.2, 85.5, None) == "yes_locked"


def test_no_lock_when_bucket_still_reachable():
    assert lock_state(75.0, 77.5, 79.5) is None        # bucket above current max
    assert lock_state(78.0, 77.5, 79.5) is None        # max inside bucket — can still rise out
    assert lock_state(80.0, 85.5, None) is None        # open-ended floor not touched


# ── gain weight & expected final max ─────────────────────────────────────────

def test_gain_weight_decays_through_the_day():
    assert gain_weight(10.0) == 1.0          # start of intraday window
    assert gain_weight(17.0) == 0.0          # end of peak window
    mid = gain_weight(13.5)
    assert 0.0 < mid < 1.0
    assert gain_weight(12.0) > gain_weight(15.0)


def test_expected_final_max_never_below_running_max():
    # forecast BELOW the running max → mu = running max (monotonicity)
    assert expected_final_max(85.0, 82.0, 12.0) == 85.0
    # no forecast at all → mu = running max
    assert expected_final_max(85.0, None, 12.0) == 85.0


def test_expected_final_max_decays_toward_running_max():
    # morning: most of the (forecast - max) gap still ahead
    early = expected_final_max(70.0, 80.0, 10.0)
    late = expected_final_max(70.0, 80.0, 16.0)
    assert early == 80.0                      # gain weight 1.0 at start hour
    assert 70.0 < late < early                # most of the gap is gone by 16:00


# ── sigma schedule ────────────────────────────────────────────────────────────

def test_sigma_shrinks_toward_peak():
    s_morning = intraday_sigma(10.0, peak_passed=False)   # 7h to peak end
    s_mid = intraday_sigma(14.0, peak_passed=False)       # 3h
    s_late = intraday_sigma(16.5, peak_passed=False)      # 0.5h
    assert s_morning > s_mid > s_late
    assert intraday_sigma(12.0, peak_passed=True) == DEFAULT_PARAMS.post_peak_sigma


# ── peak-passed detection ────────────────────────────────────────────────────

def test_peak_passed_requires_all_conditions():
    # all three conditions met
    assert is_peak_passed(15.5, 82.0, 85.0, 120.0) is True
    # too early in the day
    assert is_peak_passed(12.0, 82.0, 85.0, 120.0) is False
    # temp hasn't fallen enough
    assert is_peak_passed(15.5, 84.2, 85.0, 120.0) is False
    # max set too recently (could just be METAR noise)
    assert is_peak_passed(15.5, 82.0, 85.0, 30.0) is False
    # missing observations → never claim peak passed
    assert is_peak_passed(15.5, None, 85.0, 120.0) is False


# ── bucket probability (truncated normal) ────────────────────────────────────

def test_dead_bucket_probability_floor():
    assert bucket_probability(82.0, 82.0, 0.5, 77.5, 79.5) == PROB_LO


def test_locked_bucket_probability_ceiling():
    assert bucket_probability(86.2, 86.2, 0.5, 85.5, None) == PROB_HI


def test_bucket_containing_max_post_peak_is_near_certain():
    # running max 78.8 inside bucket 78-79 (77.5–79.5), post-peak sigma 0.3,
    # mu = running max → P = Phi((79.5 - 78.8)/0.3) = Phi(2.33) ≈ 0.99
    p = bucket_probability(78.8, 78.8, 0.3, 77.5, 79.5)
    assert p > 0.95


def test_bucket_above_max_far_from_mu_is_unlikely():
    # bucket 84-85 (83.5–85.5), max 78.8, mu 79.0, sigma 1.0 → ~0
    p = bucket_probability(78.8, 79.0, 1.0, 83.5, 85.5)
    assert p < 0.05


def test_probabilities_clip_to_valid_range():
    for lo, hi in [(None, 60.5), (60.5, 70.5), (90.5, None), (77.5, 79.5)]:
        p = bucket_probability(78.0, 79.0, 1.2, lo, hi)
        assert PROB_LO <= p <= PROB_HI


# ── end-to-end estimate ──────────────────────────────────────────────────────

def test_estimate_post_peak_lock_scenario():
    # Denver 16:30, max hit 85.1 two hours ago, temp down to 82.
    # Bucket "78-79°F" is mathematically dead → NO is near-certain.
    p, bd = estimate_intraday(
        running_max_f=85.1, current_temp_f=82.0, minutes_since_max=120.0,
        forecast_high_f=85.0, local_hour=16.5,
        bucket_min=78, bucket_max=79, bucket_unit="F",
    )
    assert p == PROB_LO
    assert bd["lock_state"] == "yes_impossible"
    assert bd["peak_passed"] is True


def test_estimate_open_ended_locked():
    p, bd = estimate_intraday(
        running_max_f=86.4, current_temp_f=86.0, minutes_since_max=20.0,
        forecast_high_f=87.0, local_hour=14.0,
        bucket_min=86, bucket_max=None, bucket_unit="F",
    )
    assert p == PROB_HI
    assert bd["lock_state"] == "yes_locked"


def test_estimate_midday_uncertainty_is_genuine():
    # 11:00, max 72, forecast 80 — bucket 78-79 genuinely uncertain: not clipped
    p, bd = estimate_intraday(
        running_max_f=72.0, current_temp_f=71.5, minutes_since_max=15.0,
        forecast_high_f=80.0, local_hour=11.0,
        bucket_min=78, bucket_max=79, bucket_unit="F",
    )
    assert PROB_LO < p < PROB_HI
    assert bd["lock_state"] is None
    assert bd["peak_passed"] is False


def test_estimate_celsius_bucket():
    # Seoul "27°C" bucket = [27, 28)°C = [80.6, 82.4)°F; max 83.2°F → dead.
    p, bd = estimate_intraday(
        running_max_f=83.2, current_temp_f=81.0, minutes_since_max=100.0,
        forecast_high_f=83.0, local_hour=15.5,
        bucket_min=27, bucket_max=27, bucket_unit="C",
    )
    assert p == PROB_LO
    assert bd["lock_state"] == "yes_impossible"


def test_params_are_tunable():
    custom = IntradayParams(start_hour=9.0, peak_end_hour=18.0)
    assert gain_weight(9.0, custom) == 1.0
    assert hours_to_peak_end(17.0, custom) == 1.0
