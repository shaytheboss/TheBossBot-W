"""Named situations the bot has to handle, as ready-made inputs.

The point of naming these is that a test should say *which* situation it is
about. `signals_full()` vs `signals_sparse()` reads as an intent; a dict
literal with seven forecast keys in it does not, and drifts the moment the
estimator gains a source.

Every builder reads `_DET_SOURCES` from the estimator itself, so adding a
model to the blend updates these fixtures instead of silently leaving the new
source absent from every test.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from app.analyzers.probability_estimator import _DET_SOURCES, _SPARSE_SOURCE_BASELINE

#: signals keys for sources that count toward the sparse-source shrink.
GLOBAL_SOURCES: tuple[str, ...] = tuple(k for k, _l, g in _DET_SOURCES if g)
#: CONUS-only sources. Absent for London — expected, and NOT a data gap.
CONUS_ONLY_SOURCES: tuple[str, ...] = tuple(k for k, _l, g in _DET_SOURCES if not g)
ALL_SOURCES: tuple[str, ...] = tuple(k for k, _l, _g in _DET_SOURCES)

TODAY = date(2026, 7, 28)
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def forecast_signal(
    high: float, low: float = 74.0, *, retrieved_at: Optional[datetime] = None
) -> dict:
    """One source's entry, shaped like `SignalAggregator._latest_forecast`."""
    return {
        "predicted_high_f": high,
        "predicted_low_f": low,
        "conditions": "Sunny",
        "retrieved_at": (retrieved_at or NOW).isoformat(),
    }


def _base_signals(bucket_min: int, bucket_max: int, market_price: float) -> dict:
    return {
        "primary_metar": None,
        "reference_metar": None,
        "metar_trend": None,
        "metar_today_max_f": None,
        "wunderground_forecast": None,
        "gfs_ensemble": None,
        "ecmwf_ensemble": None,
        "pireps": [],
        "station_bias": {},
        "model_weights": {},
        "_onshore_wind_dir": None,
        "_unavailable_api": set(),
        "market_price": market_price,
        "price_trend": None,
        "is_low_market": False,
        "_bucket_unit": "F",
        "_bucket_native_min": bucket_min,
        "_bucket_native_max": bucket_max,
        "_bucket_min": bucket_min,
        "_bucket_max": bucket_max,
        "city_lat": 30.1975,
        "city_lon": -97.6664,
    }


def signals_full(
    *,
    high: float = 95.0,
    spread: float = 0.0,
    bucket_min: int = 95,
    bucket_max: int = 96,
    market_price: float = 0.62,
    sources: Iterable[str] = ALL_SOURCES,
) -> dict:
    """Every source reporting. `spread` fans the sources apart symmetrically,
    which is the knob that moves confidence: spread=0 is unanimous agreement,
    spread=6 is genuine model disagreement."""
    sources = list(sources)
    signals = _base_signals(bucket_min, bucket_max, market_price)
    for key in ALL_SOURCES:
        signals[key] = None
    if sources:
        n = len(sources)
        for i, key in enumerate(sources):
            offset = 0.0 if n == 1 else (i / (n - 1) - 0.5) * spread
            signals[key] = forecast_signal(high + offset)
    return signals


def signals_sparse(n_global: int = 2, **kwargs) -> dict:
    """Only `n_global` global sources reporting — the collector-miss case.

    This is the path through `_SPARSE_SOURCE_SHRINK_PER_MISSING`: the estimate
    is pulled toward 0.5 by 8pp for each of the
    `_SPARSE_SOURCE_BASELINE - n_global` absent global sources. Worth testing
    explicitly, because that shrink is doing real work masking overconfidence
    and a change to it moves every sparse estimate at once.
    """
    if not 0 <= n_global <= len(GLOBAL_SOURCES):
        raise ValueError(f"n_global must be 0..{len(GLOBAL_SOURCES)}")
    return signals_full(sources=GLOBAL_SOURCES[:n_global], **kwargs)


def signals_international(**kwargs) -> dict:
    """London-shaped: global sources present, CONUS-only ones absent.

    HRRR and NWS never cover London. A correct implementation must not treat
    that as missing data, so this scenario separates "no coverage" from
    "collector failed".
    """
    return signals_full(sources=GLOBAL_SOURCES, **kwargs)


def signals_disagreement(spread: float = 8.0, **kwargs) -> dict:
    """All sources present but scattered — confidence must fall, not hold."""
    return signals_full(spread=spread, **kwargs)


def missing_global_count(signals: dict) -> int:
    """How many global sources this scenario is short of the baseline."""
    present = sum(1 for k in GLOBAL_SOURCES if signals.get(k))
    return max(0, _SPARSE_SOURCE_BASELINE - present)


# ── Market shapes ─────────────────────────────────────────────────────────

LIQUID_BOOK = {"bid": 0.61, "ask": 0.63, "spread": 0.02, "mid": 0.62}
ILLIQUID_BOOK = {"bid": 0.40, "ask": 0.80, "spread": 0.40, "mid": 0.60}
#: Below this the position sizing treats the market as untradeable.
ONE_SIDED_BOOK = None


def buckets(
    center: int = 95, count: int = 5, width: int = 2
) -> list[tuple[str, int, int]]:
    """A market's bucket ladder as (label, min, max).

    Fahrenheit buckets cover `[min, max + 1)` — the off-by-one convention
    documented on `MarketOutcome.bucket_unit`, reproduced here so tests that
    care about boundaries use the real geometry.
    """
    start = center - (count // 2) * width
    out = []
    for i in range(count):
        lo = start + i * width
        hi = lo + width - 1
        out.append((f"{lo}-{hi}°F", lo, hi))
    return out


# ── Exit-monitor situations ───────────────────────────────────────────────
# Each returns (entry, fresh) certainty/forecast pairs matching the trigger
# conditions in app/analyzers/exit_monitor.py. Tests assert the monitor fires
# on these and holds on `EXIT_NO_TRIGGER`.

#: Both legs of the primary dual trigger: >=20pp certainty drop AND >=2°F shift.
EXIT_DUAL_TRIGGER = {
    "entry_certainty": 0.92, "fresh_certainty": 0.68,
    "entry_high_f": 95.0, "fresh_high_f": 92.0,
}
#: Certainty collapses below the 0.55 floor — fires on its own.
EXIT_FLOOR_BREACH = {
    "entry_certainty": 0.91, "fresh_certainty": 0.48,
    "entry_high_f": 95.0, "fresh_high_f": 94.5,
}
#: Forecast moves >=5°F — fires on its own even if confidence holds up.
EXIT_EXTREME_SHIFT = {
    "entry_certainty": 0.93, "fresh_certainty": 0.90,
    "entry_high_f": 95.0, "fresh_high_f": 88.0,
}
#: Drifts on both axes but clears neither threshold — must NOT fire.
EXIT_NO_TRIGGER = {
    "entry_certainty": 0.92, "fresh_certainty": 0.80,
    "entry_high_f": 95.0, "fresh_high_f": 94.0,
}


def lead_times() -> list[tuple[int, date]]:
    """(days_ahead, event_date) pairs spanning the trading horizon and past it.

    `max_days_ahead_for_alert` is 3, so 0-3 are tradeable and 7 is beyond the
    horizon — the boundary the tiered price polling turns on.
    """
    return [(d, TODAY + timedelta(days=d)) for d in (0, 1, 2, 3, 7)]
