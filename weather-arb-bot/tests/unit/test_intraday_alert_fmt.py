"""Tests for the ⚡ intraday alert formatters and the bias-corrected blend.

Covers the bugs fixed in the alert-enrichment pass:
- None-safety: missing breakdown fields must never crash the formatter
- bias-corrected blended forecast (METAR vs gridded-NWP warm bias)
- lock-state / peak-passed / normal status rendering
- daily formatter using the detector's actual share count
"""
from types import SimpleNamespace

from app.bot.telegram_bot import _fmt_intraday_alert, _fmt_intraday_realert
from app.intraday.detector import blended_forecast_high


def _mk_opp(signals: dict, side="NO", conf=93, edge=0.19, shares=None, cost=None):
    return SimpleNamespace(
        signals=signals,
        side=side,
        confidence_score=conf,
        edge=edge,
        virtual_shares=shares,
        virtual_cost=cost,
    )


def _full_breakdown(**over) -> dict:
    bd = {
        "running_max_f": 55.0,
        "current_temp_f": 54.0,
        "forecast_high_f": 62.9,
        "expected_final_max_f": 62.9,
        "local_hour": 10.37,
        "hours_to_peak_end": 6.63,
        "gain_weight": 0.947,
        "sigma_used": 2.2,
        "peak_passed": False,
        "lock_state": None,
        "f_lo": 65.5,
        "f_hi": 67.5,
        "probability": 0.07,
    }
    bd.update(over)
    return bd


def _signals(bd, **over) -> dict:
    sig = {
        "_intraday": bd,
        "_book": {"bid": 0.69, "ask": 0.71, "spread": 0.02},
        "_entry_cost": 0.71,
        "_buy_threshold": 0.94,
        "_create_virtual_buy": False,
        "_forecast_sources": {"HRRR": 62.0, "NWS": 63.5, "GFS": 61.8},
        "_forecast_bias_f": 1.5,
        "_forecast_bias_is_default": True,
    }
    sig.update(over)
    return sig


# ── full alert rendering ─────────────────────────────────────────────────────

def test_full_alert_contains_all_sections():
    opp = _mk_opp(_signals(_full_breakdown()))
    text = _fmt_intraday_alert(
        opp, city_name="Seattle", bucket_label="66-67°F",
        market_question="Highest temperature in Seattle on June 10?",
        station_icao="KSEA", market_url="https://polymarket.com/event/x",
    )
    assert "INTRADAY — NO 66-67°F" in text
    assert "#INTRADAY" in text
    assert "KSEA" in text
    assert "Running max today: *55.0°F*" in text
    assert "HRRR: 62.0°F".replace(" ", "") in text.replace("*", "").replace(" ", "")
    assert "Blended high" in text
    assert "Airport bias +1.5°F" in text
    assert "YES needs: 65.5°F ≤ final max < 67.5°F" in text
    assert "σ = ±2.2°F" in text
    assert "P(YES) = 7.0%" in text
    assert "P(NO) = 93.0%" in text
    assert "bid 29¢ / ask 31¢" in text  # NO side: 1-ask / 1-bid
    assert "Edge: +19¢" in text
    assert "No virtual buy" in text
    assert "Polymarket" in text


def test_alert_buy_line_with_pnl():
    sig = _signals(_full_breakdown(), _create_virtual_buy=True)
    opp = _mk_opp(sig, shares=5, cost=3.55)
    text = _fmt_intraday_alert(opp, "Seattle", "66-67°F", "q")
    assert "INTRADAY BUY" in text
    assert "#INTRADAY_BUY" in text
    assert "5 × 71¢ = $3.55" in text
    assert "If WIN: +$1.45" in text
    assert "If LOSS: −$3.55" in text


def test_alert_lock_yes_impossible():
    bd = _full_breakdown(lock_state="yes_impossible", running_max_f=82.0, f_hi=79.5)
    text = _fmt_intraday_alert(_mk_opp(_signals(bd)), "Denver", "78-79°F", "q")
    assert "LOCK — YES IMPOSSIBLE" in text
    assert "82.0°F" in text
    assert "79.5°F" in text
    assert "NO is mathematically guaranteed" in text
    # time line is shown even in lock branches
    assert "Local time" in text


def test_alert_lock_yes_locked():
    bd = _full_breakdown(lock_state="yes_locked", running_max_f=86.2, f_lo=85.5, f_hi=None)
    text = _fmt_intraday_alert(_mk_opp(_signals(bd), side="YES"), "Phoenix", "86°F+", "q")
    assert "LOCK — YES SECURED" in text
    assert "YES is mathematically guaranteed" in text


def test_alert_peak_passed():
    bd = _full_breakdown(peak_passed=True, sigma_used=0.3)
    text = _fmt_intraday_alert(_mk_opp(_signals(bd)), "Miami", "84-85°F", "q")
    assert "Peak passed" in text
    assert "±0.3°F" in text


# ── None-safety (the crash bugs) ─────────────────────────────────────────────

def test_alert_survives_empty_breakdown():
    """Missing breakdown fields must render as placeholders, never crash."""
    opp = _mk_opp(_signals({}))
    text = _fmt_intraday_alert(opp, "Seattle", "66-67°F", "q")
    assert "N/A" in text
    assert "None°F" not in text


def test_alert_survives_missing_hours_and_gain_weight():
    bd = _full_breakdown(hours_to_peak_end=None, gain_weight=None)
    text = _fmt_intraday_alert(_mk_opp(_signals(bd)), "Seattle", "66-67°F", "q")
    assert "?h" in text


def test_realert_survives_empty_breakdown():
    ra = {
        "city_name": "Seattle", "bucket_label": "66-67°F", "side": "NO",
        "certainty": 0.93, "edge": 0.19, "entry_cost": 0.71,
        "change_note": "certainty ↑2pp", "breakdown": {},
    }
    text = _fmt_intraday_realert(ra)
    assert "INTRADAY UPDATE" in text
    assert "certainty ↑2pp" in text
    assert "?h" in text


def test_realert_full_breakdown():
    ra = {
        "city_name": "Seattle", "bucket_label": "66-67°F", "side": "NO",
        "certainty": 0.95, "edge": 0.21, "entry_cost": 0.74,
        "change_note": "certainty ↑2pp", "breakdown": _full_breakdown(),
    }
    text = _fmt_intraday_realert(ra)
    assert "6.6h to peak end" in text
    assert "now 54.0°F" in text
    assert "Certainty: *95%*" in text


# ── bias-corrected blend ─────────────────────────────────────────────────────

def test_blended_forecast_high_applies_default_bias():
    # No station_bias → +1.5°F prior applied to every source.
    signals = {
        "hrrr_forecast": {"predicted_high_f": 60.0},
        "nws_forecast": {"predicted_high_f": 60.0},
    }
    assert blended_forecast_high(signals) == 61.5


def test_blended_forecast_high_applies_learned_bias():
    signals = {
        "station_bias": {"bias_f": 2.0, "per_source": {}},
        "hrrr_forecast": {"predicted_high_f": 60.0},
    }
    assert blended_forecast_high(signals) == 62.0


def test_blended_forecast_high_weights_sources():
    # HRRR weight 2.0, GFS 1.25; default +1.5°F bias applies to both.
    signals = {
        "hrrr_forecast": {"predicted_high_f": 70.0},
        "gfs_forecast": {"predicted_high_f": 80.0},
    }
    expected = (2.0 * 71.5 + 1.25 * 81.5) / 3.25
    assert abs(blended_forecast_high(signals) - expected) < 1e-9


def test_blended_forecast_high_no_sources():
    assert blended_forecast_high({}) is None
