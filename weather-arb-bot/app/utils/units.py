"""Shared temperature-unit detection and conversion helpers.

Used by:
- app/workers/jobs.py to detect & normalise Celsius bucket labels at ingest time
- app/bot/formatters.py to render dual-unit (°F + °C) display in alerts
- app/analyzers/* to resolve effective bucket unit even before migration 005 runs
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


def resolve_bucket_unit(outcome) -> str:
    """Return the effective bucket unit ('C' or 'F') for an outcome.

    The persisted `bucket_unit` column defaults to 'F'. When migration 005
    hasn't run yet, or when an older outcome was ingested before bucket_unit
    was wired up, the column may report 'F' even though `bucket_min` stores
    native Celsius integers (parsed straight from a "°C" label).

    Falling back to label inspection guarantees downstream code (probability
    estimator, resolution, display) treats the bucket bounds in the unit
    they were actually parsed in.
    """
    unit = (getattr(outcome, "bucket_unit", None) or "F").upper()
    if unit != "C" and is_celsius_bucket(getattr(outcome, "bucket_label", None)):
        return "C"
    return unit


def c_to_f(celsius: Optional[float]) -> Optional[int]:
    if celsius is None:
        return None
    return round(celsius * 9 / 5 + 32)


def f_to_c(fahrenheit: Optional[float]) -> Optional[int]:
    if fahrenheit is None:
        return None
    return round((fahrenheit - 32) * 5 / 9)


# Temperatures resolve Polymarket buckets, so the in/out-of-bucket decision must
# be exact. The actual high is stored in °F and converted to °C for °C markets;
# that conversion introduces binary floating-point dust. For example a true
# 22.0°C high (71.6°F) converts back to 21.999999999999996°C, which is *just*
# below the 22°C bucket floor — so the comparison `22 <= 21.9999996` fails and a
# bucket that genuinely contained the actual temperature is judged a loss. That
# flips the YES/NO outcome and gets written to the DB (e.g. "22°C NO → WIN" when
# the high was exactly 22°C). We round to kill the float dust without disturbing
# any real fractional temperature (4 decimals = 0.0001°C, far finer than any
# real measurement resolution).
_BUCKET_RESOLVE_PRECISION = 4


def temp_in_bucket(
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    actual_in_unit: float,
) -> bool:
    """True iff the actual temperature falls inside the (YES) bucket.

    `bucket_min`/`bucket_max` are inclusive integer bounds in the bucket's native
    unit. A bounded bucket [bmin, bmax] covers the half-open interval
    [bmin, bmax+1) — e.g. the "22°C" bucket covers [22, 23). Open-ended buckets
    use a single bound ("22°C or higher" → bmin only; "22°C or lower" → bmax only).

    `actual_in_unit` must already be expressed in the bucket's native unit. It is
    rounded to remove float-conversion dust before comparison (see note above).
    """
    actual = round(actual_in_unit, _BUCKET_RESOLVE_PRECISION)
    if bucket_min is not None and bucket_max is not None:
        return bucket_min <= actual < bucket_max + 1
    if bucket_min is not None:
        return actual >= bucket_min
    if bucket_max is not None:
        return actual <= bucket_max
    return False



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
