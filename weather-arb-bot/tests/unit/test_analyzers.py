"""Unit tests for probability estimator and confidence scorer."""
import pytest
from app.analyzers.probability_estimator import estimate_true_probability
from app.analyzers.confidence_scorer import compute_confidence


def _base_signals(**overrides) -> dict:
    signals: dict = {
        "primary_metar": {
            "temperature_f": 63.0,
            "dew_point_f": 50.0,
            "wind_direction": 290,
            "wind_speed_kt": 8,
        },
        "reference_metar": {
            "temperature_f": 60.0,
            "wind_direction": 310,
            "wind_speed_kt": 12,
        },
        "metar_trend": {
            "temp_rate_per_hour": 0.5,
            "current_temp_f": 63.0,
            "dew_rate_per_hour": 0.0,
            "span_hours": 3.0,
        },
        "wunderground_forecast": {"predicted_high_f": 65},
        "gfs_forecast": {"predicted_high_f": 64},
        "ecmwf_forecast": {"predicted_high_f": 65},
        "pireps": [],
        "market_price": {"yes_price": 0.15, "no_price": 0.85},
        "price_trend": None,
    }
    signals.update(overrides)
    return signals


def test_forecast_in_bucket_raises_prob():
    signals = _base_signals(
        wunderground_forecast={"predicted_high_f": 65},
        gfs_forecast={"predicted_high_f": 64},
        ecmwf_forecast={"predicted_high_f": 65},
    )
    p = estimate_true_probability(signals, bucket_min=64, bucket_max=65)
    assert p > 0.50


def test_forecast_outside_bucket_lowers_prob():
    signals = _base_signals(
        wunderground_forecast={"predicted_high_f": 70},
        gfs_forecast={"predicted_high_f": 71},
        ecmwf_forecast={"predicted_high_f": 70},
    )
    p = estimate_true_probability(signals, bucket_min=64, bucket_max=65)
    assert p < 0.30


def test_coastal_wind_dampens_warm_bucket():
    signals = _base_signals(
        reference_metar={"temperature_f": 58.0, "wind_direction": 310, "wind_speed_kt": 18},
        wunderground_forecast={"predicted_high_f": 67},
    )
    p_with_wind = estimate_true_probability(signals, bucket_min=66, bucket_max=None)
    signals_no_wind = _base_signals(
        reference_metar={"temperature_f": 65.0, "wind_direction": 180, "wind_speed_kt": 5},
        wunderground_forecast={"predicted_high_f": 67},
    )
    p_no_wind = estimate_true_probability(signals_no_wind, bucket_min=66, bucket_max=None)
    assert p_with_wind < p_no_wind


def test_probability_always_in_range():
    for wg in [50, 60, 65, 70, 80]:
        signals = _base_signals(wunderground_forecast={"predicted_high_f": wg})
        p = estimate_true_probability(signals, bucket_min=64, bucket_max=65)
        assert 0.01 <= p <= 0.99


def test_models_agree_boosts_confidence():
    signals_agree = _base_signals(
        gfs_forecast={"predicted_high_f": 65},
        ecmwf_forecast={"predicted_high_f": 65},
        wunderground_forecast={"predicted_high_f": 65},
    )
    signals_disagree = _base_signals(
        gfs_forecast={"predicted_high_f": 60},
        ecmwf_forecast={"predicted_high_f": 70},
        wunderground_forecast={"predicted_high_f": 65},
    )
    c_agree = compute_confidence(signals_agree, 64, 65)
    c_disagree = compute_confidence(signals_disagree, 64, 65)
    assert c_agree > c_disagree


def test_confidence_in_valid_range():
    signals = _base_signals()
    c = compute_confidence(signals, 64, 65)
    assert 0 <= c <= 100


def test_supporting_trend_boosts_confidence():
    signals_warm = _base_signals(
        metar_trend={"temp_rate_per_hour": 2.0, "current_temp_f": 64.0, "dew_rate_per_hour": 0.0, "span_hours": 2.0},
    )
    signals_cool = _base_signals(
        metar_trend={"temp_rate_per_hour": -2.0, "current_temp_f": 64.0, "dew_rate_per_hour": 0.0, "span_hours": 2.0},
    )
    c_warm = compute_confidence(signals_warm, 66, None)
    c_cool = compute_confidence(signals_cool, 66, None)
    assert c_warm > c_cool
