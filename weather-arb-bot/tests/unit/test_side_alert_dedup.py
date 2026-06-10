"""Unit tests for side-alert dedup: open-position re-alerts fire only when
confidence changes by >= 1pp since the last sent alert. Price is no longer
a trigger."""
import app.analyzers.opportunity_detector as od


def setup_function(_fn):
    od._reset_side_dedup_if_new_day()
    od._open_position_last_sent.clear()
    od._bucket_switch_alerts_sent.clear()


def test_first_alert_always_sends():
    should, note = od._open_position_alert_due(1, "NO", 0.95)
    assert should is True
    assert note is None


def test_identical_signal_suppressed():
    od._open_position_alert_due(1, "NO", 0.95)
    should, _ = od._open_position_alert_due(1, "NO", 0.95)
    assert should is False


def test_sub_threshold_drift_suppressed():
    od._open_position_alert_due(1, "NO", 0.950)
    # 0.9pp — just below the 1pp threshold
    should, _ = od._open_position_alert_due(1, "NO", 0.959)
    assert should is False


def test_exactly_one_pp_fires():
    od._open_position_alert_due(1, "NO", 0.900)
    should, note = od._open_position_alert_due(1, "NO", 0.910)
    assert should is True
    assert "confidence" in note
    assert "↑1pp" in note


def test_confidence_drop_fires_with_note():
    od._open_position_alert_due(1, "NO", 0.95)
    should, note = od._open_position_alert_due(1, "NO", 0.91)
    assert should is True
    assert "↓4pp" in note


def test_confidence_rise_fires_with_note():
    od._open_position_alert_due(1, "NO", 0.91)
    should, note = od._open_position_alert_due(1, "NO", 0.95)
    assert should is True
    assert "↑4pp" in note


def test_baseline_updates_after_realert():
    od._open_position_alert_due(1, "NO", 0.95)
    od._open_position_alert_due(1, "NO", 0.91)   # re-alerts, new baseline 0.91
    # 0.9pp from the new baseline — suppressed
    should, _ = od._open_position_alert_due(1, "NO", 0.919)
    assert should is False
    # 1pp from the new baseline — fires
    should, _ = od._open_position_alert_due(1, "NO", 0.92)
    assert should is True


def test_keys_are_independent():
    od._open_position_alert_due(1, "NO", 0.95)
    # Different outcome → fresh key, always sends
    should, note = od._open_position_alert_due(2, "NO", 0.95)
    assert should is True and note is None
    # Same outcome, different side → also fresh
    should, note = od._open_position_alert_due(1, "YES", 0.95)
    assert should is True and note is None
