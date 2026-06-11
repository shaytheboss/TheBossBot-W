"""Pure intraday probability model. See INTRADAY.md for the full strategy.

The final daily max is modeled as max(M, X):
- M = the running METAR max so far today (monotonic, known)
- X ~ N(mu, sigma_h) = the max that remaining heating would reach

mu anchors on M plus the time-decayed share of (blended forecast high - M).
sigma_h shrinks as the local clock approaches the end of the climatological
peak window, and collapses further once the peak has demonstrably passed.

Every constant lives in IntradayParams so the learning loop can tune them.
All functions here are pure — no DB, no I/O — and fully unit-testable.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

from app.analyzers.probability_estimator import _bucket_to_f_bounds, _norm_cdf

PROB_LO = 0.015
PROB_HI = 0.985


@dataclass(frozen=True)
class IntradayParams:
    start_hour: float = 10.0        # don't run the intraday view before this local hour
    peak_start_hour: float = 14.0   # climatological window in which the max occurs
    peak_end_hour: float = 17.0
    # (hours_to_peak_end_at_least, sigma) — first matching row wins, ordered desc.
    sigma_schedule: tuple = (
        (6.0, 2.2),
        (4.0, 1.6),
        (2.0, 1.0),
        (1.0, 0.6),
        (0.0, 0.4),
    )
    post_peak_sigma: float = 0.3
    # "Peak passed" detection: current temp this far below the running max...
    cooling_drop_f: float = 1.5
    # ...for at least this long, and only after peak_start_hour.
    cooling_min_minutes: float = 90.0


DEFAULT_PARAMS = IntradayParams()


def local_decimal_hour(now_utc: datetime, tz) -> float:
    """City-local clock as a decimal hour (e.g. 14.5 = 14:30)."""
    local = now_utc.astimezone(tz)
    return local.hour + local.minute / 60.0


def hours_to_peak_end(local_hour: float, params: IntradayParams = DEFAULT_PARAMS) -> float:
    return max(0.0, params.peak_end_hour - local_hour)


def gain_weight(local_hour: float, params: IntradayParams = DEFAULT_PARAMS) -> float:
    """Fraction of the day's remaining heating still ahead.

    1.0 at start_hour, decaying linearly to 0.0 at peak_end_hour. Clamped.
    """
    span = params.peak_end_hour - params.start_hour
    if span <= 0:
        return 0.0
    w = (params.peak_end_hour - local_hour) / span
    return max(0.0, min(1.0, w))


def expected_final_max(
    running_max_f: float,
    forecast_high_f: Optional[float],
    local_hour: float,
    params: IntradayParams = DEFAULT_PARAMS,
) -> float:
    """mu of the final-max distribution. Never below the running max."""
    if forecast_high_f is None or forecast_high_f <= running_max_f:
        return running_max_f
    remaining = (forecast_high_f - running_max_f) * gain_weight(local_hour, params)
    return running_max_f + remaining


def is_peak_passed(
    local_hour: float,
    current_temp_f: Optional[float],
    running_max_f: float,
    minutes_since_max: Optional[float],
    params: IntradayParams = DEFAULT_PARAMS,
) -> bool:
    """True when the day's max is very unlikely to rise further.

    Requires all three: we're past the start of the peak window, the current
    temp has fallen well below the max, and the max was set long enough ago
    that the drop isn't just METAR noise.
    """
    if local_hour < params.peak_start_hour:
        return False
    if current_temp_f is None or minutes_since_max is None:
        return False
    return (
        running_max_f - current_temp_f >= params.cooling_drop_f
        and minutes_since_max >= params.cooling_min_minutes
    )


def intraday_sigma(
    local_hour: float,
    peak_passed: bool,
    params: IntradayParams = DEFAULT_PARAMS,
) -> float:
    if peak_passed:
        return params.post_peak_sigma
    h = hours_to_peak_end(local_hour, params)
    for min_hours, sigma in params.sigma_schedule:
        if h >= min_hours:
            return sigma
    return params.sigma_schedule[-1][1]


def lock_state(
    running_max_f: float,
    f_lo: Optional[float],
    f_hi: Optional[float],
) -> Optional[str]:
    """Deterministic outcomes already decided by the monotonic running max.

    - "yes_impossible": the running max is already above the bucket's top —
      this bucket cannot be the final answer (the max can only rise).
    - "yes_locked": open-ended ">=lo" bucket whose floor was already touched —
      it is guaranteed YES regardless of what happens next.
    """
    if f_hi is not None and running_max_f >= f_hi:
        return "yes_impossible"
    if f_hi is None and f_lo is not None and running_max_f >= f_lo:
        return "yes_locked"
    return None


def bucket_probability(
    running_max_f: float,
    mu: float,
    sigma: float,
    f_lo: Optional[float],
    f_hi: Optional[float],
) -> float:
    """P(final max lands in [f_lo, f_hi)) under final = max(M, X), X~N(mu, sigma).

    The mass of X below M collapses onto the point M (the max can't go down),
    which gives clean closed forms:
    - bucket entirely below M           -> ~0
    - bucket containing M (lo <= M < hi)-> Phi((hi - mu) / sigma)
    - bucket above M (lo > M)           -> Phi((hi-mu)/s) - Phi((lo-mu)/s)
    Open-ended tails follow the same logic with the missing bound at +/-inf.
    """
    sigma = max(sigma, 1e-6)

    state = lock_state(running_max_f, f_lo, f_hi)
    if state == "yes_impossible":
        return PROB_LO
    if state == "yes_locked":
        return PROB_HI

    if f_hi is None:
        # ">= lo" not yet touched (lo > M): P(X >= lo)
        p = 1.0 - _norm_cdf((f_lo - mu) / sigma)
    elif f_lo is None or f_lo <= running_max_f:
        # "<= hi" or bucket containing the running max: everything below hi
        # (including the point mass at M) counts.
        p = _norm_cdf((f_hi - mu) / sigma)
    else:
        p = _norm_cdf((f_hi - mu) / sigma) - _norm_cdf((f_lo - mu) / sigma)

    return max(PROB_LO, min(PROB_HI, p))


def estimate_intraday(
    running_max_f: float,
    current_temp_f: Optional[float],
    minutes_since_max: Optional[float],
    forecast_high_f: Optional[float],
    local_hour: float,
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    bucket_unit: str = "F",
    params: IntradayParams = DEFAULT_PARAMS,
    metar_max_f: Optional[float] = None,
) -> Tuple[float, dict]:
    """Full intraday estimate for one bucket. Returns (probability, breakdown).

    running_max_f is the OFFICIAL running max (may come from Wunderground, the
    resolution source). metar_max_f, when given, is the METAR-derived max used
    only for peak-passed detection — current_temp_f and minutes_since_max are
    METAR readings, so the cooling test must compare on the same scale.
    """
    f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)

    peak_detection_max = metar_max_f if metar_max_f is not None else running_max_f
    peak_passed = is_peak_passed(
        local_hour, current_temp_f, peak_detection_max, minutes_since_max, params
    )
    sigma = intraday_sigma(local_hour, peak_passed, params)
    mu = expected_final_max(running_max_f, forecast_high_f, local_hour, params)
    p = bucket_probability(running_max_f, mu, sigma, f_lo, f_hi)
    state = lock_state(running_max_f, f_lo, f_hi)

    breakdown = {
        "running_max_f": round(running_max_f, 1),
        "metar_max_f": round(metar_max_f, 1) if metar_max_f is not None else None,
        "current_temp_f": round(current_temp_f, 1) if current_temp_f is not None else None,
        "forecast_high_f": round(forecast_high_f, 1) if forecast_high_f is not None else None,
        "expected_final_max_f": round(mu, 1),
        "local_hour": round(local_hour, 2),
        "hours_to_peak_end": round(hours_to_peak_end(local_hour, params), 2),
        "gain_weight": round(gain_weight(local_hour, params), 3),
        "sigma_used": sigma,
        "peak_passed": peak_passed,
        "lock_state": state,
        "f_lo": f_lo,
        "f_hi": f_hi,
        "probability": round(p, 4),
    }
    return p, breakdown
