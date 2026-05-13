from datetime import datetime
import pytz


def to_local(dt: datetime, tz_name: str) -> datetime:
    """Convert a UTC datetime to a local timezone."""
    tz = pytz.timezone(tz_name)
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(tz)


def local_now(tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    return datetime.now(tz)


def fmt_local(dt: datetime, tz_name: str, fmt: str = "%b %d %H:%M %Z") -> str:
    return to_local(dt, tz_name).strftime(fmt)
