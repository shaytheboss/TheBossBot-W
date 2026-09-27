"""Pure functions: a day's METAR readings → its peak; solar noon. No I/O."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

#: Same rule the signal aggregator applies to today's max: a top reading more
#: than this above the second-highest is a bad observation, not the high.
SPIKE_THRESHOLD_F = 5.0

#: A day counts toward the climatology only if the readings cover it: enough
#: of them, starting by the morning, running into the evening, and with no
#: hole long enough to hide the peak.
MIN_OBS = 12
COVER_FROM_HOUR = 8.0
COVER_TO_HOUR = 20.0
MAX_GAP_H = 3.0


@dataclass
class DayPeak:
    local_date: date
    max_f: float
    peak_hour: float
    peak_last_hour: float
    n_obs: int
    first_obs_hour: float
    last_obs_hour: float
    max_gap_h: float
    spike_dropped: int

    @property
    def complete(self) -> bool:
        return is_complete(self.n_obs, self.first_obs_hour, self.last_obs_hour, self.max_gap_h)


def is_complete(n_obs, first_obs_hour, last_obs_hour, max_gap_h) -> bool:
    return (n_obs >= MIN_OBS and first_obs_hour <= COVER_FROM_HOUR
            and last_obs_hour >= COVER_TO_HOUR and max_gap_h <= MAX_GAP_H)


def _decimal_hour(t: datetime) -> float:
    return t.hour + t.minute / 60.0 + t.second / 3600.0


def group_by_local_day(readings: Iterable[tuple[datetime, float]], tz_name: str
                       ) -> dict[date, list[tuple[datetime, float]]]:
    """(UTC time, °F) → {local date: [(local time, °F), …]} in time order."""
    tz = ZoneInfo(tz_name)
    days: dict[date, list] = {}
    for t, f in readings:
        if f is None:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=ZoneInfo("UTC"))
        local = t.astimezone(tz)
        days.setdefault(local.date(), []).append((local, float(f)))
    for v in days.values():
        v.sort(key=lambda r: r[0])
    return days


def day_peak(local_date: date, readings: list[tuple[datetime, float]]) -> Optional[DayPeak]:
    """The day's high and when it came, from readings in LOCAL time."""
    if not readings:
        return None
    temps = sorted((f for _, f in readings), reverse=True)
    spike = 0
    top = temps[0]
    if len(temps) >= 2 and temps[0] - temps[1] > SPIKE_THRESHOLD_F:
        top, spike = temps[1], 1
    at_max = [t for t, f in readings if abs(f - top) < 1e-6]
    hours = [_decimal_hour(t) for t, _ in readings]
    return DayPeak(
        local_date=local_date, max_f=top,
        peak_hour=_decimal_hour(at_max[0]), peak_last_hour=_decimal_hour(at_max[-1]),
        n_obs=len(readings), first_obs_hour=hours[0], last_obs_hour=hours[-1],
        # Holes before the first and after the last reading are covered by
        # first/last_obs_hour; this is the longest hole in between.
        max_gap_h=max(b - a for a, b in zip(hours, hours[1:])) if len(hours) > 1 else 24.0,
        spike_dropped=spike,
    )


def solar_noon_hour(lat: float, lon: float, tz_name: str, day: date) -> float:
    """Local clock time of solar noon (decimal hours), DST included.

    NOAA's equation-of-time approximation (±1 minute) — the sun runs up to
    16 minutes ahead of or behind the mean clock through the year.
    """
    n = day.timetuple().tm_yday
    b = math.radians(360.0 / 365.0 * (n - 81))
    eot_min = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    offset_h = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(tz_name)) \
        .utcoffset().total_seconds() / 3600.0
    return 12.0 + (offset_h * 15.0 - lon) / 15.0 - eot_min / 60.0


def mid_month(year: int, month: int) -> date:
    return date(year, month, 15)


def closed_local_dates(first: date, now_local_date: date) -> list[date]:
    """Every local date from `first` up to, not including, the city's today."""
    out, d = [], first
    while d < now_local_date:
        out.append(d)
        d += timedelta(days=1)
    return out
