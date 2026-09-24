"""Each city's own clock.

Every row the study records is tagged with where the city is in its own day,
because a fixed UTC hour means something different in every city: at 12:00
UTC Tokyo's day is nearly over, London is at lunch, and New York has not
started. Averaging across cities at a fixed UTC time would wash out exactly the
effect the study is looking for.

The reference point is the end of the event's LOCAL calendar day — the moment
the daily high is final. Hours-to-close counts down to it in every city alike.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _zone(tz_name: Optional[str]) -> ZoneInfo:
    """The city's zone, falling back to UTC for a missing or bad name rather
    than failing the whole snapshot run over one misconfigured city."""
    try:
        return ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def hour_floor(dt: datetime) -> datetime:
    """Truncate to the hour. Makes the snapshot key idempotent within an hour."""
    return dt.replace(minute=0, second=0, microsecond=0)


def local_close_utc(event_date: date, tz_name: Optional[str]) -> datetime:
    """Midnight at the end of `event_date` in the city's zone, as UTC.

    Built from the local wall clock and then converted, so a DST change on the
    event day is handled by the zone database rather than by arithmetic.
    """
    local_midnight = datetime.combine(
        event_date + timedelta(days=1), time(0), tzinfo=_zone(tz_name)
    )
    return local_midnight.astimezone(timezone.utc)


def hours_to_close(now: datetime, event_date: date, tz_name: Optional[str]) -> float:
    """Hours from `now` until the city's event day ends. Negative once past."""
    return (local_close_utc(event_date, tz_name) - now).total_seconds() / 3600.0


def local_hour(now: datetime, tz_name: Optional[str]) -> int:
    return now.astimezone(_zone(tz_name)).hour
