"""Shared temperature-unit detection and conversion helpers.

Used by:
- app/workers/jobs.py to detect & normalise Celsius bucket labels at ingest time
- app/bot/formatters.py to render dual-unit (°F + °C) display in alerts
"""
import re
from typing import Optional

_C_PATTERN = re.compile(r"\d\s*c(?:\s|$|or\b|/)", re.IGNORECASE)


def is_celsius_bucket(label: Optional[str]) -> bool:
    """True iff the bucket label appears to be expressed in Celsius."""
    if not label:
        return False
    lo = label.lower()
    if "°c" in lo or "celsius" in lo:
        return True
    return bool(_C_PATTERN.search(lo))


def c_to_f(celsius: Optional[float]) -> Optional[int]:
    if celsius is None:
        return None
    return round(celsius * 9 / 5 + 32)


def f_to_c(fahrenheit: Optional[float]) -> Optional[int]:
    if fahrenheit is None:
        return None
    return round((fahrenheit - 32) * 5 / 9)


def fmt_temp_dual(temp_f: Optional[float], show_celsius: bool) -> str:
    """Render a temperature in °F, with °C appended when show_celsius is True."""
    if temp_f is None:
        return "—"
    f_int = round(temp_f)
    if not show_celsius:
        return f"{f_int}°F"
    return f"{f_int}°F/{f_to_c(temp_f)}°C"


def fmt_bucket_range_f(
    bucket_min: Optional[int], bucket_max: Optional[int]
) -> Optional[str]:
    if bucket_min is not None and bucket_max is not None:
        return f"{bucket_min}–{bucket_max}°F"
    if bucket_min is not None:
        return f"≥{bucket_min}°F"
    if bucket_max is not None:
        return f"≤{bucket_max}°F"
    return None


def fmt_bucket_range_c(
    bucket_min_f: Optional[int], bucket_max_f: Optional[int]
) -> Optional[str]:
    cmin = f_to_c(bucket_min_f) if bucket_min_f is not None else None
    cmax = f_to_c(bucket_max_f) if bucket_max_f is not None else None
    if cmin is not None and cmax is not None:
        return f"{cmin}–{cmax}°C"
    if cmin is not None:
        return f"≥{cmin}°C"
    if cmax is not None:
        return f"≤{cmax}°C"
    return None
