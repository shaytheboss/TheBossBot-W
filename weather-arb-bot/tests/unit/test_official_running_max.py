"""Tests for the resolution-source-aware running max (Wunderground override)
and the probability-first alert prefix.

Background (real incident): Polymarket resolves on the Wunderground station.
WU's history page showed 74°F while the METAR-derived running max said 73.0°F
— the model confidently treated 73.0 as the day's ceiling. The official max
must be the higher of the two readings (with staleness + sanity guards).
"""
from types import SimpleNamespace

from app.bot.formatters import fmt_opportunity
from app.bot.telegram_bot import (
    _fmt_bucket_switch_alert,
    _fmt_intraday_alert,
    _fmt_intraday_realert,
    _fmt_open_position_alert,
)
from app.intraday.detector import official_running_max
from app.intraday.estimator import estimate_intraday


# ── official_running_max ─────────────────────────────────────────────────────

def test_wu_higher_than_metar_wins():
    # The exact Denver incident: METAR 73.0, WU official 74.
    mx, src, suspect = official_running_max(73.0, 74.0, 30.0)
    assert mx == 74.0
    assert src == "wunderground"
    assert suspect is False


def test_metar_higher_than_wu_wins():
    mx, src, suspect = official_running_max(75.0, 74.0, 30.0)
    assert mx == 75.0
    assert src == "metar"
    assert suspect is False


def test_stale_wu_reading_ignored():
    mx, src, _ = official_running_max(73.0, 74.0, 240.0)  # 4h old
    assert mx == 73.0
    assert src == "metar"


def test_wu_far_above_metar_is_suspect_not_used():
    # +6°F gap — almost certainly a scraped forecast row, not an observation.
    mx, src, suspect = official_running_max(70.0, 76.0, 30.0)
    assert mx == 70.0
    assert src == "metar"
    assert suspect is True


def test_no_wu_data_falls_back_to_metar():
    mx, src, suspect = official_running_max(73.0, None, None)
    assert mx == 73.0
    assert src == "metar"
    assert suspect is False


def test_no_metar_returns_none():
    mx, src, _ = official_running_max(None, 74.0, 10.0)
    assert mx is None
    assert src == "none"


def test_wu_unknown_age_is_trusted_within_band():
    # No retrieved_at — still accept a plausible (+1°F) reading.
    mx, src, _ = official_running_max(73.0, 74.0, None)
    assert mx == 74.0
    assert src == "wunderground"


# ── WU max changes the lock verdict ──────────────────────────────────────────

def test_wu_override_can_kill_a_bucket():
    """METAR 73 says bucket 72-73 is alive; official WU max 74 kills it."""
    # With METAR only: max 73.0 inside bucket 72-73 (71.5-73.5) → not dead.
    p_metar, bd_metar = estimate_intraday(
        running_max_f=73.0, current_temp_f=73.0, minutes_since_max=10.0,
        forecast_high_f=74.0, local_hour=13.0,
        bucket_min=72, bucket_max=73, bucket_unit="F",
    )
    assert bd_metar["lock_state"] is None

    # With the WU official max: 74.0 >= 73.5 → yes_impossible.
    p_wu, bd_wu = estimate_intraday(
        running_max_f=74.0, current_temp_f=73.0, minutes_since_max=10.0,
        forecast_high_f=74.0, local_hour=13.0,
        bucket_min=72, bucket_max=73, bucket_unit="F",
        metar_max_f=73.0,
    )
    assert bd_wu["lock_state"] == "yes_impossible"
    assert bd_wu["metar_max_f"] == 73.0


def test_peak_detection_stays_on_metar_scale():
    """current_temp is a METAR reading — peak-passed must compare to METAR max,
    not the (higher) WU max, or a 1°F station gap fakes a cooling signal."""
    # WU max 74, METAR max 73, current 72.6 (only 0.4 below METAR max).
    # Against the WU max the drop would be 1.4°F — close to triggering.
    _, bd = estimate_intraday(
        running_max_f=74.0, current_temp_f=71.9, minutes_since_max=120.0,
        forecast_high_f=74.0, local_hour=15.0,
        bucket_min=78, bucket_max=79, bucket_unit="F",
        metar_max_f=73.0,
    )
    # drop vs METAR max = 1.1 < 1.5 → NOT peak passed (vs WU it would be 2.1)
    assert bd["peak_passed"] is False


# ── WU is observation, not forecast ──────────────────────────────────────────

def test_wu_excluded_from_intraday_forecast_blend():
    """Same-day WU value = observed-so-far high, NOT a final-high forecast.
    Blending it in dragged the expected final max toward the running max."""
    from app.intraday.detector import blended_forecast_high
    base = {"hrrr_forecast": {"predicted_high_f": 80.0}}
    with_wu = {**base, "wunderground_forecast": {"predicted_high_f": 60.0}}
    assert blended_forecast_high(base) == blended_forecast_high(with_wu)


# ── probability-first prefix on every alert type ─────────────────────────────

def _intraday_opp(conf=93):
    bd = {
        "running_max_f": 74.0, "metar_max_f": 73.0, "current_temp_f": 73.0,
        "forecast_high_f": 75.0, "expected_final_max_f": 74.5,
        "local_hour": 13.0, "hours_to_peak_end": 4.0, "gain_weight": 0.57,
        "sigma_used": 1.6, "peak_passed": False, "lock_state": None,
        "f_lo": 75.5, "f_hi": 77.5, "probability": 0.07,
        "max_source": "wunderground", "wu_high_f": 74.0, "wu_suspect": False,
    }
    sig = {
        "_intraday": bd, "_book": {"bid": 0.10, "ask": 0.13, "spread": 0.03},
        "_entry_cost": 0.90, "_buy_threshold": 0.94, "_create_virtual_buy": False,
        "_forecast_sources": {"HRRR": 75.0}, "_forecast_bias_f": 1.5,
        "_forecast_bias_is_default": True,
    }
    return SimpleNamespace(signals=sig, side="NO", confidence_score=conf,
                           edge=0.08, virtual_shares=None, virtual_cost=None)


def test_intraday_alert_starts_with_probability():
    text = _fmt_intraday_alert(_intraday_opp(93), "Denver", "76-77°F", "q")
    assert text.startswith("*93%*")


def test_intraday_realert_starts_with_probability():
    ra = {
        "city_name": "Denver", "bucket_label": "76-77°F", "side": "NO",
        "certainty": 0.98, "edge": 0.16, "entry_cost": 0.83,
        "change_note": "certainty ↑4pp", "breakdown": {},
    }
    text = _fmt_intraday_realert(ra)
    assert text.startswith("*98%*")


def test_open_position_alert_starts_with_probability():
    alert = {
        "certainty": 0.93, "calibrated_certainty": 0.93, "edge": 0.1,
        "entry_cost": 0.8, "city_name": "Denver", "side": "NO",
        "bucket_label": "76-77°F", "change_note": None,
        "event_date": __import__("datetime").date(2026, 6, 11),
    }
    assert _fmt_open_position_alert(alert).startswith("*93%*")


def test_bucket_switch_alert_starts_with_probability():
    alert = {
        "new_confidence": 91, "new_edge": 0.12, "new_entry_cost": 0.7,
        "city_name": "Denver", "new_side": "NO", "new_bucket_label": "76-77°F",
        "old_buckets": ["74-75°F"], "old_entry_prices": [0.65],
        "event_date": __import__("datetime").date(2026, 6, 11),
    }
    assert _fmt_bucket_switch_alert(alert).startswith("*91%*")


def test_daily_alert_starts_with_probability():
    text = fmt_opportunity(
        city_name="Denver", market_question="q", bucket_label="76-77°F",
        market_price=0.20, true_prob=0.07, edge=0.1, confidence=93,
        signals={"_blend": {}}, side="NO",
    )
    assert text.startswith("*93%*")


def test_intraday_alert_shows_wu_override():
    text = _fmt_intraday_alert(_intraday_opp(), "Denver", "76-77°F", "q")
    assert "Official source override" in text
    assert "74.0°F" in text
    assert "73.0°F" in text


def test_intraday_realert_shows_wu_override():
    ra = {
        "city_name": "Denver", "bucket_label": "76-77°F", "side": "NO",
        "certainty": 0.98, "edge": 0.16, "entry_cost": 0.83,
        "change_note": "certainty ↑4pp",
        "breakdown": {
            "running_max_f": 74.0, "metar_max_f": 73.0,
            "expected_final_max_f": 74.0, "hours_to_peak_end": 4.0,
            "sigma_used": 1.0, "max_source": "wunderground", "wu_high_f": 74.0,
        },
    }
    text = _fmt_intraday_realert(ra)
    assert "Wunderground (resolution station)" in text
    assert "74.0°F" in text
