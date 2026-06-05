"""Unit tests for per-model forecast extraction and accuracy scoring.

These cover the dashboard's "which model predicted correctly" feature:
- pulling each model's captured point forecast out of an opportunity's signals,
- scoring a forecast (°F) against the bucket Polymarket settled, for both
  Fahrenheit and Celsius buckets,
- treating an unresolved market (no winning bucket) as "unknown" (None).
"""
from app.api.admin import (
    _extract_model_forecasts,
    _forecast_f_in_bucket,
    _score_model_forecasts,
)


def test_extract_skips_missing_and_null():
    signals = {
        "gfs_forecast": {"predicted_high_f": 85.0},
        "ecmwf_forecast": {"predicted_high_f": 86.4},
        "hrrr_forecast": {"predicted_high_f": None},   # no data → skipped
        "nws_forecast": {"predicted_high_f": 84.2},
        "tomorrowio_forecast": None,                    # not reporting → skipped
        "gfs_ensemble": {"p50_high_f": 86.0},
    }
    mf = _extract_model_forecasts(signals)
    assert mf == {"GFS": 85.0, "ECMWF": 86.4, "NWS": 84.2, "GFS-ens": 86.0}


def test_extract_handles_non_dict():
    assert _extract_model_forecasts(None) == {}
    assert _extract_model_forecasts("nope") == {}


def test_fahrenheit_bucket_scoring():
    # Polymarket settled the 86-87°F bucket (covers [86, 88)).
    winners = [("F", 86, 87)]
    assert _forecast_f_in_bucket(86.4, winners) is True
    assert _forecast_f_in_bucket(86.0, winners) is True
    assert _forecast_f_in_bucket(85.0, winners) is False   # below
    assert _forecast_f_in_bucket(88.0, winners) is False   # above (exclusive top)


def test_celsius_bucket_scoring():
    # Polymarket settled the 27°C bucket (covers [27, 28)°C).
    winners = [("C", 27, 27)]
    assert _forecast_f_in_bucket(80.6, winners) is True    # 80.6°F == 27.0°C
    assert _forecast_f_in_bucket(87.8, winners) is False   # 87.8°F == 31.0°C


def test_open_ended_bucket():
    winners = [("F", 92, None)]  # "92°F or higher"
    assert _forecast_f_in_bucket(95.0, winners) is True
    assert _forecast_f_in_bucket(90.0, winners) is False


def test_unresolved_market_is_none():
    assert _forecast_f_in_bucket(85.0, []) is None


def test_score_annotates_each_model():
    mf = {"GFS": 85.0, "ECMWF": 86.4}
    scored = _score_model_forecasts(mf, [("F", 86, 87)])
    assert scored["GFS"] == {"f": 85.0, "correct": False}
    assert scored["ECMWF"] == {"f": 86.4, "correct": True}
    # Unresolved → correct is None for every model.
    scored_open = _score_model_forecasts(mf, [])
    assert scored_open["GFS"]["correct"] is None
