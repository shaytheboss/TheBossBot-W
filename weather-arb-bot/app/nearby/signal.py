"""The signal itself — pure functions, no I/O.

The rule is exactly the one chosen on July-August history and then checked,
untouched, on September and on October-November:

  - at least MIN_NEARBY neighbours with a fresh reading today;
  - at least MIN_FALLING_SHARE of them already FALL_F or more below their own
    high of the day;
  - the bucket holding the settlement station's high so far trades at a mid
    between MID_LO and MID_HI;
  - (added after the first live days, tested on the whole history) it is
    past the city's usual peak hour — see PEAK_SHARE_MIN.

Net of a modelled spread it made +6.5 / +4.0 / +4.5 c/share in the three
periods, but only when bought within ~5 minutes: the market caught up within
the hour, and the one check at our own recorded asks was negative. This module
exists to measure that with live books, on paper.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

MIN_NEARBY = 2
MIN_FALLING_SHARE = 0.6
FALL_F = 1.0
MID_LO, MID_HI = 0.30, 0.85
# Only after the city's usual peak: at least this share of its days (same
# month, daily_peaks) had their high by the current local hour. Of the first
# ten live signals, the two that lost had fired before the peak (San
# Francisco, Los Angeles: the settlement station was still climbing). On
# Jul-Nov history the condition lifted the rule from +6.5/+4.0/+4.5 c/share
# (Jul-Aug/Sep/Oct-Nov) to +7.4/+8.3/+7.0, the win rate from ~71% to ~79%,
# out-of-sample t 1.85 -> 2.96.
PEAK_SHARE_MIN = 0.5
# A buy is counted only when the real ask is below what the bucket has been
# worth: how often, after the peak, a holding bucket at this mid went on to
# win (Jul-Aug history; Sep-Nov matched within a few points), less MARGIN.
# Replaces a flat spread cap: a 16c spread is fine at mid 75c (fair 85c, ask
# 83c), and a 99.5c ask is never fine (Munich, first day live).
FAIR_BY_MID = ((0.50, 0.538), (0.60, 0.667), (0.70, 0.735), (0.80, 0.850), (0.85, 0.915))
MARGIN = 0.02
MIN_READINGS = 3
STALE = timedelta(minutes=80)


@dataclass
class StationState:
    icao: str
    now_f: float
    high_f: float
    last_at: datetime

    @property
    def below_f(self) -> float:
        return self.high_f - self.now_f


def station_state(icao: str, readings: list[tuple[datetime, float]],
                  now: datetime) -> Optional[StationState]:
    """Readings of ONE local day, any order. None when too few or stale."""
    rs = sorted((t, v) for t, v in readings if v is not None and t <= now)
    if len(rs) < MIN_READINGS or now - rs[-1][0] > STALE:
        return None
    return StationState(icao=icao, now_f=float(rs[-1][1]), high_f=max(float(v) for _, v in rs),
                        last_at=rs[-1][0])


@dataclass
class Verdict:
    target: StationState
    nearby: list[StationState]
    falling: list[StationState]

    @property
    def share(self) -> float:
        return len(self.falling) / len(self.nearby) if self.nearby else 0.0

    @property
    def fires(self) -> bool:
        return len(self.nearby) >= MIN_NEARBY and self.share >= MIN_FALLING_SHARE


def evaluate(target: Optional[StationState], nearby: list[Optional[StationState]]) -> Optional[Verdict]:
    if target is None:
        return None
    live = [s for s in nearby if s is not None]
    return Verdict(target=target, nearby=live, falling=[s for s in live if s.below_f >= FALL_F])


def holds(bucket_min, bucket_max, unit: str, temp_f: float) -> bool:
    """Does this bucket hold `temp_f`? The reading is rounded to a whole degree
    of the bucket's unit, which matched the settled bucket on 98.7% of 1,309
    US station-days (the truncating rule did not)."""
    v = (temp_f - 32) * 5 / 9 if unit == "C" else temp_f
    r = round(v)
    if bucket_min is not None and bucket_max is not None:
        return bucket_min <= r <= bucket_max
    if bucket_min is not None:
        return r >= bucket_min
    if bucket_max is not None:
        return r <= bucket_max
    return False


def fair_value(mid: Optional[float]) -> Optional[float]:
    """What a holding bucket at this mid has been worth after the peak."""
    if mid is None or mid < MID_LO or mid > MID_HI:
        return None
    for upper, fair in FAIR_BY_MID:
        if mid <= upper:
            return fair
    return None


def tradable(bid: Optional[float], ask: Optional[float]) -> bool:
    """Could the paper buy be made at a price worth paying?"""
    if bid is None or ask is None or ask < bid:
        return False
    fair = fair_value((bid + ask) / 2)
    return fair is not None and ask <= fair - MARGIN


def past_peak(share: Optional[float]) -> bool:
    return share is not None and share >= PEAK_SHARE_MIN


def mid_in_band(mid: Optional[float]) -> bool:
    return mid is not None and MID_LO <= mid <= MID_HI
