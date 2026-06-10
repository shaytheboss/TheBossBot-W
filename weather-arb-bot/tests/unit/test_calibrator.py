"""Unit tests for the empirical calibrator."""
from app.analyzers.calibrator import calibrate, _band


def test_band_grouping():
    assert _band(91) == 90
    assert _band(92) == 92
    assert _band(93) == 92
    assert _band(88) == 88
    assert _band(100) == 100


def test_calibrate_no_table():
    # Empty table → raw certainty unchanged
    assert calibrate(0.91, {}) == 0.91
    assert calibrate(0.95, {}) == 0.95


def test_calibrate_no_band_entry():
    # Table exists but this band has no entry → unchanged
    table = {88: (0.80, 20)}
    assert calibrate(0.91, table) == 0.91


def test_calibrate_pulls_toward_empirical():
    # 64.2% empirical win rate for 90-91% band → calibrated moves down
    table = {90: (0.642, 35)}
    cal = calibrate(0.91, table)
    assert cal < 0.91
    assert cal > 0.642  # never collapses to raw empirical

def test_calibrate_strong_history_dominates():
    # With 200 samples the blend weight approaches MAX_BLEND (0.60)
    table = {90: (0.642, 200)}
    cal = calibrate(0.91, table)
    # Should be closer to 0.642 than to 0.91
    assert abs(cal - 0.642) < abs(cal - 0.91)


def test_calibrate_perfect_model_unchanged():
    # If empirical matches raw, calibration leaves it unchanged
    table = {90: (0.91, 50)}
    assert abs(calibrate(0.91, table) - 0.91) < 0.001


def test_calibrate_clamped_to_valid_range():
    # Even an extreme empirical rate shouldn't produce values < 0 or > 1
    table = {90: (0.10, 100)}
    cal = calibrate(0.91, table)
    assert 0.0 <= cal <= 1.0
