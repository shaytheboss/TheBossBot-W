"""Unit tests for the virtual exit monitor.

Tests cover:
  - _should_trigger_exit: all trigger conditions (dual, floor, extreme shift, no-trigger)
  - _extract_entry_forecast_high: nested signal extraction, missing keys, malformed data
  - _extract_entry_certainty: confidence_score → certainty conversion
  - _fmt_exit_alert: message format sanity (contains key fields)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from app.analyzers.exit_monitor import (
    _should_trigger_exit,
    _extract_entry_forecast_high,
    _extract_entry_certainty,
    _normalize_estimator,
    EXIT_CONFIDENCE_DROP_PP,
    EXIT_FORECAST_SHIFT_F,
    EXIT_CERTAINTY_FLOOR,
    EXIT_FORECAST_SHIFT_EXTREME_F,
)


# ── _should_trigger_exit ──────────────────────────────────────────────────────

class TestShouldTriggerExit:
    """All trigger conditions for _should_trigger_exit."""

    def test_no_trigger_when_both_below_threshold(self):
        """Confidence drop < threshold AND shift < threshold → no exit."""
        should, reason = _should_trigger_exit(
            entry_certainty=0.90,
            fresh_certainty=0.80,   # 10pp drop — below EXIT_CONFIDENCE_DROP_PP=20
            forecast_shift_f=1.0,   # below EXIT_FORECAST_SHIFT_F=2.0
        )
        assert not should
        assert reason == ""

    def test_no_trigger_large_drop_but_small_shift(self):
        """20pp+ drop but tiny forecast shift → not enough for dual trigger."""
        should, reason = _should_trigger_exit(
            entry_certainty=0.92,
            fresh_certainty=0.70,   # 22pp drop
            forecast_shift_f=0.5,   # below 2°F threshold
        )
        assert not should

    def test_no_trigger_large_shift_but_small_drop(self):
        """2°F+ forecast shift but < 20pp confidence drop → not enough."""
        should, reason = _should_trigger_exit(
            entry_certainty=0.90,
            fresh_certainty=0.82,   # 8pp drop — below 20pp threshold
            forecast_shift_f=3.0,   # above shift threshold but below extreme
        )
        assert not should

    def test_dual_trigger_fires(self):
        """Both conditions met: 20pp+ drop AND 2°F+ shift → exit."""
        should, reason = _should_trigger_exit(
            entry_certainty=0.90,
            fresh_certainty=0.68,   # 22pp drop
            forecast_shift_f=2.5,   # above 2°F threshold
        )
        assert should
        assert "confidence" in reason.lower() or "dropped" in reason.lower()

    def test_dual_trigger_exact_boundary(self):
        """Exactly at threshold values: should trigger."""
        should, _ = _should_trigger_exit(
            entry_certainty=0.90,
            fresh_certainty=0.90 - EXIT_CONFIDENCE_DROP_PP / 100.0,
            forecast_shift_f=EXIT_FORECAST_SHIFT_F,
        )
        assert should

    def test_floor_breach_triggers_alone(self):
        """Certainty below EXIT_CERTAINTY_FLOOR → exit regardless of shift."""
        should, reason = _should_trigger_exit(
            entry_certainty=0.80,
            fresh_certainty=EXIT_CERTAINTY_FLOOR - 0.01,
            forecast_shift_f=0.0,   # no forecast shift at all
        )
        assert should
        assert "floor" in reason.lower() or "collapsed" in reason.lower()

    def test_floor_breach_exact_boundary(self):
        """Exactly at floor — still triggers (below means <)."""
        should, _ = _should_trigger_exit(
            entry_certainty=0.80,
            fresh_certainty=EXIT_CERTAINTY_FLOOR - 0.001,
            forecast_shift_f=0.0,
        )
        assert should

    def test_no_trigger_above_floor(self):
        """Certainty above floor, small drop, small shift → no exit."""
        should, _ = _should_trigger_exit(
            entry_certainty=0.80,
            fresh_certainty=EXIT_CERTAINTY_FLOOR + 0.01,
            forecast_shift_f=1.0,
        )
        assert not should

    def test_extreme_shift_triggers_alone(self):
        """Forecast shifts >= EXIT_FORECAST_SHIFT_EXTREME_F → exit alone."""
        should, reason = _should_trigger_exit(
            entry_certainty=0.90,
            fresh_certainty=0.88,   # only 2pp drop
            forecast_shift_f=EXIT_FORECAST_SHIFT_EXTREME_F,
        )
        assert should
        assert "extreme" in reason.lower() or "shifted" in reason.lower()

    def test_negative_extreme_shift_triggers(self):
        """Negative extreme shift (forecast drops sharply) also triggers."""
        should, reason = _should_trigger_exit(
            entry_certainty=0.90,
            fresh_certainty=0.88,
            forecast_shift_f=-EXIT_FORECAST_SHIFT_EXTREME_F,
        )
        assert should

    def test_exactly_at_extreme_threshold(self):
        should, _ = _should_trigger_exit(
            entry_certainty=0.90,
            fresh_certainty=0.88,
            forecast_shift_f=EXIT_FORECAST_SHIFT_EXTREME_F,
        )
        assert should

    def test_reason_mentions_confidence_in_dual_trigger(self):
        should, reason = _should_trigger_exit(
            entry_certainty=0.92,
            fresh_certainty=0.65,
            forecast_shift_f=3.0,
        )
        assert should
        assert "%" in reason or "pp" in reason

    def test_no_exit_when_fresh_certainty_equals_entry(self):
        """No change at all → definitely no exit."""
        should, _ = _should_trigger_exit(0.85, 0.85, 0.0)
        assert not should

    def test_certainty_improvement_no_exit(self):
        """Certainty got better → no exit."""
        should, _ = _should_trigger_exit(
            entry_certainty=0.80,
            fresh_certainty=0.95,
            forecast_shift_f=3.0,
        )
        assert not should


# ── _extract_entry_forecast_high ─────────────────────────────────────────────

class TestExtractEntryForecastHigh:
    def _opp(self, signals):
        opp = MagicMock()
        opp.signals = signals
        return opp

    def test_happy_path(self):
        opp = self._opp({"_beta_breakdown": {"forecast_high_f": 85.0}})
        assert _extract_entry_forecast_high(opp) == pytest.approx(85.0)

    def test_alpha_blend_key(self):
        """Alpha stores the breakdown under '_blend', not '_beta_breakdown'."""
        opp = self._opp({"_blend": {"forecast_high_f": 73.5}})
        assert _extract_entry_forecast_high(opp) == pytest.approx(73.5)

    def test_beta_key_takes_priority_when_both_present(self):
        """If both keys exist, beta breakdown wins (beta rows never carry _blend,
        but be deterministic regardless)."""
        opp = self._opp({
            "_beta_breakdown": {"forecast_high_f": 80.0},
            "_blend": {"forecast_high_f": 60.0},
        })
        assert _extract_entry_forecast_high(opp) == pytest.approx(80.0)

    def test_alpha_blend_missing_forecast_returns_none(self):
        opp = self._opp({"_blend": {"final": 0.7}})
        assert _extract_entry_forecast_high(opp) is None

    def test_integer_value(self):
        opp = self._opp({"_beta_breakdown": {"forecast_high_f": 72}})
        assert _extract_entry_forecast_high(opp) == pytest.approx(72.0)

    def test_string_value_is_cast(self):
        opp = self._opp({"_beta_breakdown": {"forecast_high_f": "78.5"}})
        assert _extract_entry_forecast_high(opp) == pytest.approx(78.5)

    def test_missing_breakdown_key_returns_none(self):
        opp = self._opp({"some_other_key": {}})
        assert _extract_entry_forecast_high(opp) is None

    def test_missing_forecast_high_f_returns_none(self):
        opp = self._opp({"_beta_breakdown": {"blended_prob": 0.7}})
        assert _extract_entry_forecast_high(opp) is None

    def test_none_signals_returns_none(self):
        opp = self._opp(None)
        assert _extract_entry_forecast_high(opp) is None

    def test_empty_signals_returns_none(self):
        opp = self._opp({})
        assert _extract_entry_forecast_high(opp) is None

    def test_none_value_returns_none(self):
        opp = self._opp({"_beta_breakdown": {"forecast_high_f": None}})
        assert _extract_entry_forecast_high(opp) is None


# ── _normalize_estimator ──────────────────────────────────────────────────────

class TestNormalizeEstimator:
    def test_none_is_alpha(self):
        """Legacy rows with NULL estimator are treated as alpha."""
        assert _normalize_estimator(None) == "alpha"

    def test_blank_is_alpha(self):
        assert _normalize_estimator("") == "alpha"

    def test_beta_preserved(self):
        assert _normalize_estimator("beta") == "beta"

    def test_alpha_preserved(self):
        assert _normalize_estimator("alpha") == "alpha"

    def test_uppercase_normalized(self):
        assert _normalize_estimator("BETA") == "beta"


# ── _extract_entry_certainty ──────────────────────────────────────────────────

class TestExtractEntryCertainty:
    def _opp(self, confidence_score):
        opp = MagicMock()
        opp.confidence_score = confidence_score
        return opp

    def test_typical_value(self):
        opp = self._opp(90)
        assert _extract_entry_certainty(opp) == pytest.approx(0.90)

    def test_zero_confidence(self):
        opp = self._opp(0)
        assert _extract_entry_certainty(opp) == pytest.approx(0.0)

    def test_none_confidence_defaults_to_zero(self):
        opp = self._opp(None)
        assert _extract_entry_certainty(opp) == pytest.approx(0.0)

    def test_100_confidence(self):
        opp = self._opp(100)
        assert _extract_entry_certainty(opp) == pytest.approx(1.0)

    def test_midpoint(self):
        opp = self._opp(55)
        assert _extract_entry_certainty(opp) == pytest.approx(0.55)


# ── _fmt_exit_alert message content ──────────────────────────────────────────

class TestFmtExitAlert:
    """Smoke tests for the Telegram message formatter."""

    def _call(self, **overrides):
        from app.bot.telegram_bot import _fmt_exit_alert
        from datetime import date
        defaults = dict(
            city_name="New York",
            market_question="Will NYC high exceed 85°F?",
            bucket_label="85–89°F",
            side="YES",
            event_date=date(2026, 7, 4),
            entry_confidence=90,
            exit_confidence=65,
            forecast_shift_f=-3.5,
            trigger_reason="confidence dropped 25pp AND forecast shifted -3.5°F",
            theoretical_exit_price=0.72,
            theoretical_pnl=-0.9,
            entry_price=0.88,
            market_url="https://polymarket.com/event/test",
        )
        defaults.update(overrides)
        return _fmt_exit_alert(**defaults)

    def test_contains_city_name(self):
        assert "New York" in self._call()

    def test_contains_exit_signal_header(self):
        text = self._call()
        assert "EXIT SIGNAL" in text

    def test_beta_tag(self):
        text = self._call(estimator="beta")
        assert "[β]" in text

    def test_alpha_tag(self):
        text = self._call(estimator="alpha")
        assert "[α]" in text

    def test_null_estimator_defaults_to_alpha_tag(self):
        text = self._call(estimator=None)
        assert "[α]" in text

    def test_contains_confidence_numbers(self):
        text = self._call()
        assert "90" in text and "65" in text

    def test_contains_forecast_shift(self):
        text = self._call()
        assert "3.5" in text

    def test_contains_side(self):
        assert "YES" in self._call()

    def test_none_pnl_omitted(self):
        text = self._call(theoretical_pnl=None)
        assert "P&L" not in text and "Theoretical" not in text

    def test_none_market_url_omitted(self):
        text = self._call(market_url=None)
        assert "Market" not in text and "polymarket" not in text

    def test_contains_virtual_disclaimer(self):
        text = self._call()
        assert "VIRTUAL" in text or "virtual" in text

    def test_contains_entry_price(self):
        text = self._call()
        assert "88" in text  # 0.88 → 88¢

    def test_contains_exit_price(self):
        text = self._call()
        assert "72" in text  # 0.72 → 72¢


# ── Trigger threshold constant sanity ────────────────────────────────────────

class TestThresholdConstants:
    def test_floor_is_sane(self):
        assert 0.40 <= EXIT_CERTAINTY_FLOOR <= 0.70

    def test_confidence_drop_is_sane(self):
        assert 10 <= EXIT_CONFIDENCE_DROP_PP <= 40

    def test_forecast_shift_is_sane(self):
        assert 1.0 <= EXIT_FORECAST_SHIFT_F <= 5.0

    def test_extreme_shift_greater_than_normal(self):
        assert EXIT_FORECAST_SHIFT_EXTREME_F > EXIT_FORECAST_SHIFT_F

    def test_floor_not_too_aggressive(self):
        """Floor below 0.5 would exit every YES bet that inverts — too aggressive."""
        assert EXIT_CERTAINTY_FLOOR >= 0.50
