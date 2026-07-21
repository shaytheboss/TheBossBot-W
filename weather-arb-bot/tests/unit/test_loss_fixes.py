"""Tests for the five loss-prevention fixes (PR after days of losses).

1. WU-anchored bias — tested via the interface contract (too DB-heavy for unit)
2. Calibration gate — calibrate() reducing certainty blocks virtual buy signals
3. Near-money bucket warning — formatter shows warning when _is_near_money=True
4. Double-position prevention — _has_open_position exists and checks virtual_status
5. METAR spike rejection — official_running_max bidirectional guard (see test_official_running_max.py)
"""
from types import SimpleNamespace
from datetime import date

import pytest

from app.analyzers.calibrator import calibrate, _band
from app.bot.formatters import fmt_opportunity


# ── Fix 2: calibration gate ───────────────────────────────────────────────────

def test_calibrate_returns_raw_when_table_empty():
    assert calibrate(0.95, {}) == 0.95


def test_calibrate_blends_toward_empirical():
    # Band 94 (94-95): 50 samples, empirical win rate 0.70
    table = {94: (0.70, 50)}
    blended = calibrate(0.95, table)
    # weight = min(0.60, 50/(50+50)) = 0.50
    # blended = 0.95 * 0.50 + 0.70 * 0.50 = 0.825
    assert abs(blended - 0.825) < 0.001


def test_calibrate_skips_band_with_no_entry():
    table = {90: (0.85, 20)}
    # certainty 0.95 → band 94, which has no entry → raw returned unchanged
    assert calibrate(0.95, table) == 0.95


def test_calibrate_caps_max_blend():
    # Very large sample: weight should be capped at MAX_BLEND_WEIGHT=0.60
    table = {90: (0.60, 10000)}
    blended = calibrate(0.91, table)
    # weight = 0.60 (capped), empirical = 0.60
    # blended = 0.91 * 0.40 + 0.60 * 0.60 = 0.364 + 0.36 = 0.724
    expected = round(0.91 * 0.40 + 0.60 * 0.60, 4)
    assert abs(blended - expected) < 0.001


def test_band_rounds_down_to_even():
    assert _band(91) == 90
    assert _band(92) == 92
    assert _band(93) == 92
    assert _band(94) == 94
    assert _band(95) == 94


# Validate that a raw 95% signal that calibrates to 84% would NOT fire a buy
# at a 90% threshold — this is the key fix.
def test_calibration_would_gate_high_raw_low_empirical():
    """95% raw, 70% empirical → calibrated 82.5% — below 90% buy threshold."""
    # n=50 → weight=0.50; band 94: (0.70, 50)
    table = {94: (0.70, 50)}
    calibrated = calibrate(0.95, table)
    assert calibrated < 0.90, (
        f"Calibrated certainty {calibrated:.3f} should be below 90% buy threshold"
    )


# ── Fix 3: near-money bucket warning in formatter ────────────────────────────

def _make_signals(is_near_money=False, calibration_gated=False):
    return {
        "_blend": {
            "days_ahead": 0,
            "sigma_used": 2.5,
            "is_open_ended": False,
            "deterministic": [],
            "ensemble": None,
            "wunderground": None,
            "det_avg": None,
            "ens_p": None,
            "wg_p": None,
            "blend_before_adjustments": 0.93,
            "boundary_risk": None,
            "model_disagreement": None,
            "adjustments": [],
            "forecast_std_dev_f": None,
            "ci_pp": None,
            "final": 0.93,
            "normalization_scale": None,
            "normalized_final": 0.93,
            "has_forecast_data": True,
            "bias_correction": {"bias_f": 1.5, "is_default": True, "samples": 0, "notes": ""},
            "missing_sources": [],
            "missing_no_key": [],
            "missing_conus_only": [],
            "student_t_df": 6,
            "observation_skipped": False,
            "bucket_unit": "F",
            "is_low_market": False,
            "ensemble_models": [],
            "ensemble_std_f": None,
        },
        "_book": {"bid": 0.08, "ask": 0.10, "spread": 0.02},
        "_entry_cost": 0.92,
        "_alert_threshold": 0.90,
        "_buy_threshold": 0.90,
        "_create_virtual_buy": True,
        "_city_blacklisted": False,
        "_city_suspended": False,
        "_suspension_reason": None,
        "_calibrated_confidence": 93,
        "_shares_per_buy": 5,
        "_is_near_money": is_near_money,
        "_calibration_gated": calibration_gated,
    }


def test_near_money_warning_shown_when_flag_set():
    signals = _make_signals(is_near_money=True)
    text = fmt_opportunity(
        city_name="Denver",
        market_question="What will Denver's high be on Jun 15?",
        bucket_label="92-93°F",
        market_price=0.10,
        true_prob=0.07,
        edge=0.08,
        confidence=93,
        signals=signals,
        side="NO",
        event_date=date(2026, 6, 15),
    )
    assert "NEAR-MONEY BUCKET" in text
    assert "1-2°F error" in text


def test_near_money_warning_absent_when_flag_not_set():
    signals = _make_signals(is_near_money=False)
    text = fmt_opportunity(
        city_name="Denver",
        market_question="What will Denver's high be on Jun 15?",
        bucket_label="96-97°F",
        market_price=0.05,
        true_prob=0.04,
        edge=0.08,
        confidence=93,
        signals=signals,
        side="NO",
        event_date=date(2026, 6, 15),
    )
    assert "NEAR-MONEY BUCKET" not in text


def test_calibration_caution_when_gated_but_buy_opened():
    """Calibration is display-only: when the flag is set BUT a virtual buy was
    actually opened, the footer must be a caution — never the false 'no virtual
    buy' claim it used to print."""
    signals = _make_signals(calibration_gated=True)   # _create_virtual_buy=True
    signals["_calibrated_confidence"] = 88
    text = fmt_opportunity(
        city_name="Denver",
        market_question="What will Denver's high be on Jun 15?",
        bucket_label="92-93°F",
        market_price=0.10,
        true_prob=0.07,
        edge=0.08,
        confidence=93,
        signals=signals,
        side="NO",
        event_date=date(2026, 6, 15),
    )
    assert "Calibration caution" in text
    assert "no virtual buy" not in text


def test_calibration_gate_block_when_no_buy():
    """When the buy truly was NOT created, the real block wording is shown."""
    signals = _make_signals(calibration_gated=True)
    signals["_calibrated_confidence"] = 88
    signals["_create_virtual_buy"] = False
    signals["_city_blacklisted"] = True
    text = fmt_opportunity(
        city_name="Denver",
        market_question="What will Denver's high be on Jun 15?",
        bucket_label="92-93°F",
        market_price=0.10,
        true_prob=0.07,
        edge=0.08,
        confidence=93,
        signals=signals,
        side="NO",
        event_date=date(2026, 6, 15),
    )
    assert "Calibration gate" in text
    assert "no virtual buy" in text


def test_calibration_gate_note_absent_when_not_gated():
    signals = _make_signals(calibration_gated=False)
    text = fmt_opportunity(
        city_name="Denver",
        market_question="What will Denver's high be on Jun 15?",
        bucket_label="96-97°F",
        market_price=0.05,
        true_prob=0.04,
        edge=0.08,
        confidence=93,
        signals=signals,
        side="NO",
        event_date=date(2026, 6, 15),
    )
    assert "Calibration gate" not in text


# ── Fix 3b: near-money detector reads a TEMPERATURE, not a probability ────────

def test_breakdown_exposes_forecast_high_as_temperature():
    """forecast_high_f must be the blended forecast HIGH (°F), distinct from
    det_avg which is a probability in [0,1]. The near-money detector locates the
    bucket the forecast lands in, so it must read a temperature — reading det_avg
    (a probability) silently disabled the temperature path and always fell back
    to the highest-probability bucket."""
    from app.analyzers.probability_estimator import (
        estimate_with_breakdown,
        _bucket_contains,
    )
    signals = {
        "gfs_forecast": {"predicted_high_f": 88.0},
        "ecmwf_forecast": {"predicted_high_f": 89.0},
        "station_bias": {"bias_f": 1.5, "per_source": {}},
    }
    _, b = estimate_with_breakdown(signals, 92, 93, days_ahead=0, bucket_unit="F")

    # forecast_high_f is a plausible temperature, NOT a probability.
    assert b["forecast_high_f"] is not None
    assert b["forecast_high_f"] > 50.0
    # det_avg is a probability — the value the old buggy code read by mistake.
    assert 0.0 <= b["det_avg"] <= 1.0
    # The forecast high (≈90°F) is found inside its true bucket, but a probability
    # value never would be.
    assert _bucket_contains(b["forecast_high_f"], 89, 91, "F")
    assert not _bucket_contains(b["det_avg"], 89, 91, "F")


# ── Fix 4: _has_open_position exists and has correct signature ────────────────

def test_has_open_position_importable():
    """Verify the function exists — DB behaviour is tested in integration tests."""
    from app.analyzers.opportunity_detector import _has_open_position
    import inspect
    sig = inspect.signature(_has_open_position)
    params = list(sig.parameters)
    assert "outcome_id" in params
    assert "side" in params
