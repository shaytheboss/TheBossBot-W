"""Tests for the Telegram alert display-bug fixes (Jul 18 round):

1. "7/5 global models" — coverage numerator over-counted CONUS-only models.
2. Risk line said "virtual position opened" even when no buy was created.
3. Calibration footer claimed "no virtual buy" while the buy was actually made
   (calibration is display-only and does not block the buy).

These are display-only fixes — no model math changes. We assert on the exact
strings the formatter emits.
"""
import pytest

from app.bot.formatters import _risk_scorecard, fmt_opportunity
from app.analyzers.probability_estimator import (
    estimate_with_breakdown, _SPARSE_SOURCE_BASELINE,
)


# ── Bug 1: coverage counts GLOBAL models only, capped at baseline ──────────────

class TestCoverageCount:
    def _det(self, n):
        return [{"label": f"m{i}"} for i in range(n)]

    def test_full_coverage_reads_breakdown_field(self):
        """All 5 global + 2 CONUS present → must show 5/5, never 7/5."""
        blend = {
            "deterministic": self._det(7),          # 7 total (incl HRRR+NWS)
            "n_global_det": 5,
            "n_global_baseline": 5,
            # sparse_sources absent — the full-coverage case that used to break
        }
        out = _risk_scorecard(blend, "NO", 0.04, 0, None, False, 0.67, 0.75, 0.90)
        assert "5/5 global models" in out
        assert "7/5" not in out

    def test_legacy_sparse_dict_still_works(self):
        blend = {
            "deterministic": self._det(6),
            "sparse_sources": {"n_global_det": 3, "baseline": 5},
        }
        out = _risk_scorecard(blend, "NO", 0.1, 1, None, False, 0.6, 0.75, 0.90)
        assert "3/5 global models" in out

    def test_fallback_never_exceeds_baseline(self):
        """No explicit count anywhere → fall back but cap at baseline (not 7)."""
        blend = {"deterministic": self._det(7)}   # nothing else
        out = _risk_scorecard(blend, "NO", 0.1, 1, None, False, 0.6, 0.75, 0.90)
        assert "5/5 global models" in out
        assert "7/5" not in out

    def test_partial_coverage(self):
        blend = {"deterministic": self._det(3), "n_global_det": 3, "n_global_baseline": 5}
        out = _risk_scorecard(blend, "NO", 0.1, 2, None, False, 0.6, 0.75, 0.90)
        assert "3/5 global models" in out

    def test_estimator_exposes_global_fields(self):
        """The estimator must always populate the coverage fields, even on a
        signals dict with no forecast data at all."""
        _p, bd = estimate_with_breakdown({}, 70, 71, days_ahead=0, bucket_unit="F")
        assert "n_global_det" in bd
        assert bd["n_global_baseline"] == _SPARSE_SOURCE_BASELINE
        assert bd["n_global_det"] == 0   # empty signals → no sources


# ── Bug 2: risk line reflects the REAL buy decision ───────────────────────────

class TestBuyLineMatchesReality:
    BLEND = {"deterministic": [{"label": "x"}], "n_global_det": 5, "n_global_baseline": 5}

    def _row(self, **kw):
        return _risk_scorecard(
            self.BLEND, "NO", 0.04, 0, None, False, 0.67, 0.75, 0.90, **kw
        )

    def test_opened_when_buy_true(self):
        out = self._row(virtual_buy_opened=True)
        assert "virtual position opened" in out

    def test_no_buy_when_false(self):
        out = self._row(virtual_buy_opened=False, no_buy_reason="city blacklisted")
        assert "no virtual buy" in out
        assert "city blacklisted" in out
        assert "virtual position opened" not in out

    def test_legacy_none_keeps_opened_wording(self):
        """Callers that don't pass the status keep the old text (back-compat)."""
        out = self._row()   # virtual_buy_opened defaults to None
        assert "virtual position opened" in out

    def test_suspended_reason_shown(self):
        out = self._row(virtual_buy_opened=False, no_buy_reason="city suspended")
        assert "no virtual buy" in out and "city suspended" in out


# ── Bug 3: calibration footer honesty via fmt_opportunity ─────────────────────

class TestCalibrationFooter:
    def _signals(self, **over):
        det_row = {
            "source": "GFS (global)", "value_f": 66.0, "raw_value_f": 66.0,
            "p_in_bucket": 0.10,
        }
        s = {
            "_blend": {
                "deterministic": [det_row],
                "n_global_det": 5, "n_global_baseline": 5,
                "final": 0.96, "det_avg": 0.906,
            },
            "market_price": {"yes_price": 0.05},
            "_entry_cost": 0.67,
            "_alert_threshold": 0.75, "_buy_threshold": 0.90,
            "_calibrated_confidence": 84,
            "_calibration_gated": True,
            "_bucket_min": 70, "_bucket_max": 71,
        }
        s.update(over)
        return s

    def _fmt(self, signals):
        from datetime import date
        return fmt_opportunity(
            city_name="San Francisco", market_question="Highest temp?",
            bucket_label="70-71°F", market_price=0.24, true_prob=0.04,
            edge=0.29, confidence=96, signals=signals, side="NO",
            event_date=date(2026, 7, 18), resolution_time=None,
            market_url=None, station_icao="KSFO", city_timezone="America/Los_Angeles",
        )

    def test_gated_but_bought_is_caution_not_block(self):
        """Buy WAS created + calibration gated → caution wording, never
        the false 'no virtual buy' claim."""
        out = self._fmt(self._signals(_create_virtual_buy=True))
        assert "Calibration caution" in out
        assert "no virtual buy" not in out

    def test_gated_and_not_bought_is_real_block(self):
        out = self._fmt(self._signals(_create_virtual_buy=False, _city_blacklisted=True))
        assert "no virtual buy" in out

    def test_not_gated_no_footer(self):
        out = self._fmt(self._signals(_calibration_gated=False, _create_virtual_buy=True))
        assert "Calibration caution" not in out
        assert "Calibration gate" not in out

    def test_no_75_coverage_bug_in_full_alert(self):
        """End-to-end: the rendered alert must not contain '7/5'."""
        out = self._fmt(self._signals(_create_virtual_buy=True))
        assert "7/5" not in out
        assert "5/5 global models" in out
