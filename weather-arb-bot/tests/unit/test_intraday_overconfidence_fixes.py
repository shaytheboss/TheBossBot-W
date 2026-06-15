"""Regression tests for the four field incidents reported on 2026-06-12.

1. Tokyo: 96% YES BUY on a bucket whose floor the running max was sitting on,
   with sources spanning 4.7°F and the peak window not yet open → lost.
   Fixes: sigma floor from model disagreement + pre-peak YES cap at 90%.
2. Guangzhou: three ⚡ UPDATE messages inside 30 minutes (93→94→96) from pure
   sigma time-decay drift. Fix: 2pp threshold + 15-minute cooldown.
3. Chicago: 🔄 BUCKET SWITCH told the user to abandon a 95% NO for an 83% NO
   on a sibling bucket — NO positions on different buckets are complementary,
   not alternatives. Fix: switch alerts are YES-side only (+ Entry 0¢ fix).
4. Seoul: Celsius market rendered entirely in °F, and the "[lo, hi)" bounds
   line lost its "[" to Telegram's Markdown link parser.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.intraday.detector as idet
from app.bot.telegram_bot import (
    _fmt_bucket_switch_alert,
    _fmt_intraday_alert,
    _fmt_intraday_realert,
)
from app.intraday.estimator import DEFAULT_PARAMS, estimate_intraday


# ── 1. Tokyo overconfidence ──────────────────────────────────────────────────

TOKYO = dict(
    running_max_f=78.8, current_temp_f=78.8, minutes_since_max=10.0,
    forecast_high_f=77.8, local_hour=13.17,
    bucket_min=26, bucket_max=26, bucket_unit="C",   # [78.8, 80.6)°F
)


def test_tokyo_regression_no_more_96pct_buy():
    """Exact field scenario: spread 4.7°F, 13:10 local, M on the bucket floor.
    Old model said P(YES)=96.4% → auto-buy → loss. Now ≤90%, below buy 94%."""
    p, bd = estimate_intraday(**TOKYO, forecast_spread_f=4.7)
    assert p <= 0.90 + 1e-9
    assert bd["pre_peak_cap_applied"] or bd["sigma_floor_from_spread"] > 0
    # certainty 90% still alerts (>= intraday_min_certainty_alert) but the
    # 94% buy threshold is out of reach — alert-only, exactly the intent.


def test_sigma_combines_schedule_and_forecast_error_in_quadrature():
    """תיקון פריז: כש-μ נשען על תחזית, σ חייב לכלול את שגיאת התחזית
    באופן יחסי לתלות (w) — שני מקורות בלתי-תלויים מחוברים ריבועית."""
    _, bd = estimate_intraday(**TOKYO)
    w = bd["gain_weight"]
    expected_fc_term = w * DEFAULT_PARAMS.same_day_forecast_sigma
    assert bd["sigma_forecast_term"] == pytest.approx(expected_fc_term, abs=1e-3)
    quadrature_sigma = (bd["sigma_schedule"] ** 2 + expected_fc_term ** 2) ** 0.5
    # Celsius market: σ is additionally floored at celsius_min_sigma_f (1.8°F).
    expected_sigma = max(quadrature_sigma, DEFAULT_PARAMS.celsius_min_sigma_f)
    assert bd["sigma_used"] == pytest.approx(expected_sigma, abs=1e-2)
    # הסיגמה האפקטיבית גדולה מלוח-הזמנים לבדו — זה כל הרעיון
    assert bd["sigma_used"] > bd["sigma_schedule"]


def test_sigma_floor_binds_only_when_spread_is_huge():
    """רצפת אי-ההסכמה (טוקיו) עדיין קיימת — אבל עכשיו היא נדרסת רק כשהפיזור
    גדול מספיק כדי לעבור את איבר שגיאת-התחזית הריבועי."""
    _, bd_moderate = estimate_intraday(**TOKYO, forecast_spread_f=4.7)
    # פיזור 4.7: הרצפה (w·4.7·0.5≈1.29) קטנה מהשילוב הריבועי (≈1.69) — לא קובעת
    assert bd_moderate["sigma_used"] > bd_moderate["sigma_floor_from_spread"]
    _, bd_huge = estimate_intraday(**TOKYO, forecast_spread_f=9.0)
    # פיזור 9.0: הרצפה (≈2.48) גדולה מהשילוב — היא שקובעת את הסיגמה
    assert bd_huge["sigma_used"] == pytest.approx(
        bd_huge["sigma_floor_from_spread"], abs=1e-3
    )
    assert bd_huge["sigma_used"] > bd_moderate["sigma_used"]


def test_sigma_floor_ignored_after_peak_passed():
    p, bd = estimate_intraday(
        running_max_f=85.0, current_temp_f=82.0, minutes_since_max=120.0,
        forecast_high_f=85.0, local_hour=16.0,
        bucket_min=84, bucket_max=85, bucket_unit="F",
        forecast_spread_f=6.0,
    )
    assert bd["peak_passed"] is True
    assert bd["sigma_used"] == DEFAULT_PARAMS.post_peak_sigma


def test_pre_peak_cap_only_before_peak_window():
    # Same knife-edge bucket at 15:00 (inside peak window) — no cap.
    late = dict(TOKYO, local_hour=15.0)
    p, bd = estimate_intraday(**late)
    assert bd["pre_peak_cap_applied"] is False


def test_pre_peak_cap_does_not_touch_locks():
    # yes_locked open-ended bucket before peak start keeps PROB_HI.
    p, bd = estimate_intraday(
        running_max_f=86.4, current_temp_f=86.0, minutes_since_max=20.0,
        forecast_high_f=87.0, local_hour=12.0,
        bucket_min=86, bucket_max=None, bucket_unit="F",
    )
    assert p > 0.98
    assert bd["pre_peak_cap_applied"] is False


def test_pre_peak_cap_does_not_touch_no_side():
    # Strong NO (p tiny) before peak start must stay strong — cap is YES-only.
    p, bd = estimate_intraday(
        running_max_f=70.0, current_temp_f=69.0, minutes_since_max=30.0,
        forecast_high_f=72.0, local_hour=12.0,
        bucket_min=84, bucket_max=85, bucket_unit="F",
    )
    assert p < 0.10
    assert bd["pre_peak_cap_applied"] is False


# ── 2. Guangzhou realert noise ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_realert_state():
    idet._last_alerted.clear()
    yield
    idet._last_alerted.clear()


def _t0(minutes=0):
    return datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_realert_needs_2pp_not_1pp():
    idet._realert_due(1, "NO", 0.93, now_utc=_t0())          # baseline
    ok, _ = idet._realert_due(1, "NO", 0.94, now_utc=_t0(20))  # +1pp → silent
    assert ok is False
    ok, note = idet._realert_due(1, "NO", 0.95, now_utc=_t0(40))  # +2pp → fires
    assert ok is True and note == "certainty ↑2pp"


def test_realert_cooldown_blocks_rapid_fire():
    """The Guangzhou sequence: material moves but minutes apart must not spam."""
    idet._realert_due(2, "NO", 0.93, now_utc=_t0())
    ok, _ = idet._realert_due(2, "NO", 0.96, now_utc=_t0(10))   # +3pp but 10min
    assert ok is False
    # Baseline NOT advanced by the blocked attempt — after the cooldown the
    # sustained move still alerts.
    ok, note = idet._realert_due(2, "NO", 0.96, now_utc=_t0(16))
    assert ok is True and note == "certainty ↑3pp"


def test_realert_first_occurrence_registers_silently():
    ok, note = idet._realert_due(3, "YES", 0.95, now_utc=_t0())
    assert ok is True and note is None


# ── 3. Chicago bucket-switch semantics ───────────────────────────────────────

def test_bucket_switch_formatter_shows_old_confidences_and_real_entry():
    alert = {
        "new_confidence": 83, "new_edge": 0.24, "new_entry_cost": 0.59,
        "city_name": "Chicago", "new_side": "YES", "new_bucket_label": "80-81°F",
        "old_buckets": ["82-83°F", "82-83°F"],
        "old_entry_prices": [0.78, 0.77],
        "old_confidences": [95, 94],
        "event_date": __import__("datetime").date(2026, 6, 12),
    }
    text = _fmt_bucket_switch_alert(alert)
    assert "Entry: 59¢" in text          # not 0¢
    assert "conf 95%" in text
    assert "conf 94%" in text
    assert "Only one bucket can resolve YES" in text


def test_bucket_switch_formatter_tolerates_missing_old_confidences():
    alert = {
        "new_confidence": 91, "new_edge": 0.12, "new_entry_cost": 0.7,
        "city_name": "Denver", "new_side": "YES", "new_bucket_label": "76-77°F",
        "old_buckets": ["74-75°F"], "old_entry_prices": [0.65],
        "event_date": __import__("datetime").date(2026, 6, 12),
    }
    text = _fmt_bucket_switch_alert(alert)
    assert "opened at 65¢" in text


# ── 4. Seoul Celsius rendering ───────────────────────────────────────────────

def _seoul_opp():
    bd = {
        "running_max_f": 75.2, "metar_max_f": 75.2, "current_temp_f": 75.2,
        "forecast_high_f": 79.9, "expected_final_max_f": 79.9,
        "local_hour": 10.0, "hours_to_peak_end": 7.0, "gain_weight": 1.0,
        "sigma_used": 2.2, "peak_passed": False, "lock_state": "yes_impossible",
        "f_lo": 73.4, "f_hi": 75.2, "probability": 0.015,
        "max_source": "metar", "wu_high_f": None, "wu_suspect": False,
    }
    sig = {
        "_intraday": bd, "_book": {"bid": 0.09, "ask": 0.10, "spread": 0.01},
        "_entry_cost": 0.91, "_buy_threshold": 0.94, "_create_virtual_buy": True,
        "_forecast_sources": {"GFS": 80.9, "ECMWF": 80.6},
        "_forecast_bias_f": 6.9, "_forecast_bias_is_default": False,
        "_bucket_unit": "C",
    }
    return SimpleNamespace(signals=sig, side="NO", confidence_score=98,
                           edge=0.08, virtual_shares=5, virtual_cost=4.55)


def test_celsius_city_shows_celsius_everywhere():
    text = _fmt_intraday_alert(_seoul_opp(), "Seoul", "23°C", "q")
    # Key temperatures carry °C alongside °F
    assert "75.2°F (24.0°C)" in text
    assert "(27.2°C)" in text or "80.9°F (27.2°C)" in text  # GFS row
    # Bounds line: dual-unit and bracket-free
    assert "73.4°F (23.0°C) ≤ final max < 75.2°F (24.0°C)" in text
    assert "[" not in text.replace("[Polymarket]", "")  # only the link may use [


def test_lock_wording_handles_equality():
    # Seoul: running max == ceiling exactly — "exceeds" was wrong.
    text = _fmt_intraday_alert(_seoul_opp(), "Seoul", "23°C", "q")
    assert "has reached or passed" in text


def test_fahrenheit_city_stays_fahrenheit_only():
    opp = _seoul_opp()
    opp.signals["_bucket_unit"] = "F"
    text = _fmt_intraday_alert(opp, "Denver", "76-77°F", "q")
    assert "°C" not in text


def test_realert_celsius():
    ra = {
        "city_name": "Guangzhou", "bucket_label": "33°C", "side": "NO",
        "certainty": 0.93, "edge": 0.07, "entry_cost": 0.86,
        "change_note": "certainty ↓4pp",
        "breakdown": {
            "running_max_f": 87.8, "expected_final_max_f": 89.9,
            "current_temp_f": 87.8, "hours_to_peak_end": 2.8,
            "sigma_used": 1.0, "peak_passed": False, "lock_state": None,
        },
    }
    text = _fmt_intraday_realert(ra)
    assert "87.8°F (31.0°C)" in text
