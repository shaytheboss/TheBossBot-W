"""Unit tests for METAR parser."""
import pytest
from datetime import datetime, timezone
from app.utils.metar_parser import parse_metar_json


def _record(**kwargs):
    base = {
        "obsTime": "2026-04-24T18:56:00Z",
        "temp": 18.0,
        "dewp": 10.0,
        "wdir": 310,
        "wspd": 12,
        "wgst": None,
        "altim": 1013.2,
        "visib": 10.0,
        "wxString": "",
        "sky": "FEW018",
        "rawOb": "KSFO 241856Z 31012KT 10SM FEW018 18/10 A2993 RMK AO2",
    }
    base.update(kwargs)
    return base


def test_basic_parse():
    result = parse_metar_json(_record())
    assert result["temperature_f"] == pytest.approx(64.4, abs=0.2)
    assert result["dew_point_f"] == pytest.approx(50.0, abs=0.2)
    assert result["wind_direction"] == 310
    assert result["wind_speed_kt"] == 12
    assert result["wind_gust_kt"] is None
    assert result["pressure_hg"] == pytest.approx(29.92, abs=0.05)
    assert result["visibility_sm"] == 10.0


def test_humidity_range():
    result = parse_metar_json(_record(temp=20.0, dewp=20.0))
    assert result["humidity_pct"] == 100

    result = parse_metar_json(_record(temp=40.0, dewp=-10.0))
    assert result["humidity_pct"] < 20


def test_missing_fields():
    result = parse_metar_json(_record(temp=None, wdir=None))
    assert result["temperature_f"] is None
    assert result["wind_direction"] is None


def test_timestamp_parsing():
    result = parse_metar_json(_record(obsTime="2026-04-24T18:56:00Z"))
    assert isinstance(result["observed_at"], datetime)
    assert result["observed_at"].tzinfo is not None


def test_conditions():
    result = parse_metar_json(_record(wxString="-RA", sky="OVC010"))
    assert "-RA" in (result["conditions"] or "")
