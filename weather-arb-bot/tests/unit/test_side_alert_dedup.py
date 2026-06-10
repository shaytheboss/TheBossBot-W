"""Unit tests for side-alert dedup: open-position re-alerts fire only on
material change (confidence >= 3pp or entry price >= 5 cents)."""
import app.analyzers.opportunity_detector as od


def setup_function(_fn):
    # Each test starts with a clean dedup state for "today"
    od._reset_side_dedup_if_new_day()
    od._open_position_last_sent.clear()
    od._bucket_switch_alerts_sent.clear()


def test_first_alert_always_sends():
    should, note = od._open_position_alert_due(1, "NO", 0.95, 0.75)
    assert should is True
    assert note is None


def test_identical_signal_suppressed():
    od._open_position_alert_due(1, "NO", 0.95, 0.75)
    should, _ = od._open_position_alert_due(1, "NO", 0.95, 0.75)
    assert should is False


def test_tiny_drift_suppressed():
    od._open_position_alert_due(1, "NO", 0.95, 0.75)
    # 1pp confidence + 2¢ price — below both thresholds
    should, _ = od._open_position_alert_due(1, "NO", 0.96, 0.77)
    assert should is False


def test_confidence_jump_realerts_with_note():
    od._open_position_alert_due(1, "NO", 0.95, 0.75)
    should, note = od._open_position_alert_due(1, "NO", 0.91, 0.75)
    assert should is True
    assert "confidence" in note
    assert "↓4pp" in note


def test_price_move_realerts_with_note():
    od._open_position_alert_due(1, "NO", 0.95, 0.75)
    should, note = od._open_position_alert_due(1, "NO", 0.95, 0.82)
    assert should is True
    assert "entry price" in note
    assert "↑7¢" in note


def test_baseline_updates_after_realert():
    od._open_position_alert_due(1, "NO", 0.95, 0.75)
    od._open_position_alert_due(1, "NO", 0.91, 0.75)   # re-alerts, new baseline 0.91
    # 1pp from the NEW baseline — suppressed
    should, _ = od._open_position_alert_due(1, "NO", 0.92, 0.75)
    assert should is False
    # 3pp from the new baseline — fires again
    should, _ = od._open_position_alert_due(1, "NO", 0.94, 0.75)
    assert should is True


def test_keys_are_independent():
    od._open_position_alert_due(1, "NO", 0.95, 0.75)
    # Different outcome → fresh key, always sends
    should, note = od._open_position_alert_due(2, "NO", 0.95, 0.75)
    assert should is True and note is None
    # Same outcome, different side → also fresh
    should, note = od._open_position_alert_due(1, "YES", 0.95, 0.75)
    assert should is True and note is None
