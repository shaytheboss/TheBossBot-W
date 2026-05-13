"""
Parse the Aviation Weather JSON METAR response into a flat dict.
Aviation Weather API returns obsTime as a Unix timestamp (int), not ISO string.
Wind direction can be 'VRB' (variable) — stored as None.
"""
from datetime import datetime, timezone
from typing import Optional
import math


def _c_to_f(c: Optional[float]) -> Optional[float]:
    if c is None:
        return None
    return round(c * 9 / 5 + 32, 1)


def _dewpoint_to_humidity(temp_f: Optional[float], dew_f: Optional[float]) -> Optional[int]:
    if temp_f is None or dew_f is None:
        return None
    temp_c = (temp_f - 32) * 5 / 9
    dew_c = (dew_f - 32) * 5 / 9
    rh = 100 * math.exp(17.625 * dew_c / (243.04 + dew_c)) / math.exp(17.625 * temp_c / (243.04 + temp_c))
    return min(100, max(0, round(rh)))


def _parse_timestamp(value) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _parse_int(value) -> Optional[int]:
    """Parse int, returning None for non-numeric values like 'VRB'."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_metar_json(record: dict) -> dict:
    temp_c = record.get("temp")
    dew_c = record.get("dewp")
    temp_f = _c_to_f(temp_c)
    dew_f = _c_to_f(dew_c)

    observed_at = _parse_timestamp(record.get("obsTime") or record.get("reportTime"))

    altim = record.get("altim")
    pressure_hg = round(altim * 0.02953, 2) if altim else None

    visib = record.get("visib")
    wxstring = record.get("wxString") or record.get("wx_string") or ""
    sky = record.get("sky") or record.get("skyCondition") or ""
    if isinstance(sky, list):
        sky = " ".join(str(s) for s in sky)
    conditions = " ".join(filter(None, [wxstring, sky])).strip() or None

    return {
        "observed_at": observed_at,
        "temperature_f": temp_f,
        "dew_point_f": dew_f,
        "humidity_pct": _dewpoint_to_humidity(temp_f, dew_f),
        "wind_direction": _parse_int(record.get("wdir")),
        "wind_speed_kt": _parse_int(record.get("wspd")),
        "wind_gust_kt": _parse_int(record.get("wgst")),
        "pressure_hg": pressure_hg,
        "visibility_sm": float(visib) if visib is not None else None,
        "conditions": conditions,
        "raw_metar": record.get("rawOb") or record.get("raw_text"),
    }
