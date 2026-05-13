"""
Parse the Aviation Weather JSON METAR response into a flat dict.
Aviation Weather API returns camelCase fields.
"""
from datetime import datetime, timezone
from typing import Optional


def _c_to_f(c: Optional[float]) -> Optional[float]:
    if c is None:
        return None
    return round(c * 9 / 5 + 32, 1)


def _dewpoint_to_humidity(temp_f: Optional[float], dew_f: Optional[float]) -> Optional[int]:
    """Approximate relative humidity from temp and dew point (Magnus formula)."""
    if temp_f is None or dew_f is None:
        return None
    temp_c = (temp_f - 32) * 5 / 9
    dew_c = (dew_f - 32) * 5 / 9
    rh = 100 * (
        (17.625 * dew_c / (243.04 + dew_c)).real.__class__(1) *
        __import__("math").exp(17.625 * dew_c / (243.04 + dew_c)) /
        __import__("math").exp(17.625 * temp_c / (243.04 + temp_c))
    )
    return min(100, max(0, round(rh)))


def parse_metar_json(record: dict) -> dict:
    """Convert Aviation Weather API JSON record to our internal dict."""
    import math

    temp_c = record.get("temp")
    dew_c = record.get("dewp")
    temp_f = _c_to_f(temp_c)
    dew_f = _c_to_f(dew_c)

    obs_time_str = record.get("obsTime") or record.get("reportTime")
    if obs_time_str:
        try:
            observed_at = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
        except ValueError:
            observed_at = datetime.now(timezone.utc)
    else:
        observed_at = datetime.now(timezone.utc)

    altim = record.get("altim")
    pressure_hg = round(altim * 0.02953, 2) if altim else None

    visib = record.get("visib")

    wdir = record.get("wdir")
    wspd = record.get("wspd")
    wgst = record.get("wgst")

    wxstring = record.get("wxString") or record.get("wx_string") or ""
    sky = record.get("sky") or record.get("skyCondition") or ""
    conditions = " ".join(filter(None, [wxstring, sky])).strip() or None

    return {
        "observed_at": observed_at,
        "temperature_f": temp_f,
        "dew_point_f": dew_f,
        "humidity_pct": _dewpoint_to_humidity(temp_f, dew_f),
        "wind_direction": int(wdir) if wdir is not None else None,
        "wind_speed_kt": int(wspd) if wspd is not None else None,
        "wind_gust_kt": int(wgst) if wgst is not None else None,
        "pressure_hg": pressure_hg,
        "visibility_sm": float(visib) if visib is not None else None,
        "conditions": conditions,
        "raw_metar": record.get("rawOb") or record.get("raw_text"),
    }
