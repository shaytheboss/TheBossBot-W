"""The read side of daily_peaks that the intraday detector uses.

The one place trading code touches app/peaks. It answers a single question:
on what share of this city's days in this month had the high already come by
this local hour? Measured on 500 settled intraday trades (Jun-Jul, 14:00 or
later): entered when fewer than half the days had peaked, they won 65%
against 78% otherwise — z=3.0, in both halves of a time split, and within
cities (z=-2.8). The bot's stated confidence was the same in both (94-95%).

Read-only and cached per city for the day: the detector runs every five
minutes, the table changes once a day. Any failure returns None, which never
blocks a trade.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from sqlalchemy import select

from app.peaks.compute import is_complete
from app.peaks.models import DailyPeak
from app.peaks.report import MIN_DAYS_MEASURED

logger = logging.getLogger(__name__)

# city_id → (the day it was read, {month: sorted peak hours of complete days})
_CACHE: dict[int, tuple[date, dict[int, list[float]]]] = {}


def reset_cache() -> None:
    _CACHE.clear()


async def _peak_hours(db, city_id: int, today: date) -> dict[int, list[float]]:
    hit = _CACHE.get(city_id)
    if hit and hit[0] == today:
        return hit[1]
    rows = (await db.execute(
        select(DailyPeak.local_date, DailyPeak.peak_hour, DailyPeak.n_obs,
               DailyPeak.first_obs_hour, DailyPeak.last_obs_hour, DailyPeak.max_gap_h)
        .where(DailyPeak.city_id == city_id)
    )).all()
    by_month: dict[int, list[float]] = {}
    for d, ph, n, first, last, gap in rows:
        if is_complete(n, first, last, gap):
            by_month.setdefault(d.month, []).append(float(ph))
    for v in by_month.values():
        v.sort()
    _CACHE[city_id] = (today, by_month)
    return by_month


async def share_passed(db, city_id: int, month: int, local_hour: float,
                       today: Optional[date] = None) -> tuple[Optional[float], int]:
    """(share of complete days in `month` whose high came by `local_hour`,
    number of such days). Share is None below MIN_DAYS_MEASURED days, or if
    the read fails."""
    try:
        hours = (await _peak_hours(db, city_id, today or date.today())).get(month, [])
    except Exception as e:
        logger.warning(f"[peak guard] read failed for city {city_id}: {e}")
        return None, 0
    n = len(hours)
    if n < MIN_DAYS_MEASURED:
        return None, n
    return sum(1 for h in hours if h <= local_hour) / n, n
