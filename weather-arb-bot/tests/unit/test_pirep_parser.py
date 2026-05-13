"""Unit tests for PIREP parser."""
import pytest
from app.utils.pirep_parser import parse_pirep_text


def test_full_pirep():
    raw = "UA /OV KSFO045020 /TM 1430 /FL080 /TP B737 /TA -05 /WV 27040KT /TB LGT /IC LGT RIME"
    result = parse_pirep_text(raw)
    assert result is not None
    assert result["flight_level_ft"] == 8000
    assert result["aircraft_type"] == "B737"
    assert result["temperature_c"] == pytest.approx(-5.0)
    assert result["wind_direction"] == 270
    assert result["wind_speed_kt"] == 40
    assert result["turbulence"] == "LGT"
    assert "RIME" in result["icing"]


def test_minimal_pirep():
    raw = "UA /OV KSFO /TM 1215 /FL050 /TA 10"
    result = parse_pirep_text(raw)
    assert result is not None
    assert result["flight_level_ft"] == 5000
    assert result["temperature_c"] == 10.0


def test_empty_raw():
    assert parse_pirep_text("") is None
    assert parse_pirep_text(None) is None


def test_location_offset():
    raw = "UA /OV KSFO045020 /TM 1430 /FL060 /TA 5"
    result = parse_pirep_text(raw)
    assert result["location_offset"] == "KSFO045020"


def test_json_fallback_timestamp():
    raw = "UA /OV KSFO /FL040 /TA 8"
    record = {"obsTime": "2026-04-24T14:30:00Z"}
    result = parse_pirep_text(raw, record)
    assert result is not None
    assert result["observed_at"].hour == 14
