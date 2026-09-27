"""Fill daily_peaks from the METAR history. Scheduled every 3 hours.

Each city's day closes at a different UTC hour, so the job simply records
every closed local day it has not recorded yet, and recomputes the last
RECOMPUTE_DAYS in case late observations arrived. The first run backfills
everything the METAR table still holds (hard pruning is off by default).

Per city and run it reads two columns of METAR, only from the last recorded
day onward: a few dozen rows once the backfill is done.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.metar import MetarObservation
from app.peaks.compute import day_peak, group_by_local_day
from app.peaks.models import DailyPeak

logger = logging.getLogger(__name__)

RECOMPUTE_DAYS = 2
BACKFILL_DAYS = 400
#: Values typed into primary_icao as placeholders (Lucknow had "ICAO").
_PLACEHOLDERS = frozenset({"ICAO", "XXXX", "NONE", "NULL", "TODO", "TBD"})

LAST_RUN: dict = {}


def _local_midnight_utc(d, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0), tzinfo=tz).astimezone(timezone.utc)


async def record_city(db, city, now: datetime) -> int:
    tz = ZoneInfo(city.timezone or "UTC")
    icao = (city.primary_icao or "").upper()
    if len(icao) != 4 or icao in _PLACEHOLDERS:
        return 0
    local_today = now.astimezone(tz).date()
    last = (await db.execute(
        select(func.max(DailyPeak.local_date)).where(DailyPeak.city_id == city.id)
    )).scalar()
    start = (last - timedelta(days=RECOMPUTE_DAYS)) if last else local_today - timedelta(days=BACKFILL_DAYS)
    rows = (await db.execute(
        select(MetarObservation.observed_at, MetarObservation.temperature_f).where(
            MetarObservation.icao == icao,
            MetarObservation.observed_at >= _local_midnight_utc(start, tz),
            MetarObservation.observed_at < _local_midnight_utc(local_today, tz),
            MetarObservation.temperature_f.isnot(None),
        )
    )).all()
    written = 0
    for d, readings in group_by_local_day(rows, city.timezone or "UTC").items():
        if d >= local_today:
            continue                      # the city's day is not over yet
        p = day_peak(d, readings)
        if p is None:
            continue
        await db.merge(DailyPeak(
            city_id=city.id, local_date=d, icao=icao, max_f=p.max_f,
            peak_hour=p.peak_hour, peak_last_hour=p.peak_last_hour, n_obs=p.n_obs,
            first_obs_hour=p.first_obs_hour, last_obs_hour=p.last_obs_hour,
            max_gap_h=p.max_gap_h, spike_dropped=p.spike_dropped, computed_at=now,
        ))
        written += 1
    await db.commit()
    return written


async def record_peaks(db, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    # Plain copies, not ORM objects: after one city fails, the rollback expires
    # every loaded instance, and reading an expired attribute in async
    # SQLAlchemy is an implicit query — MissingGreenlet, and every later city
    # lost. (The shadow study hit the same trap.)
    cities = [SimpleNamespace(id=c.id, name=c.name, timezone=c.timezone,
                              primary_icao=c.primary_icao)
              for c in (await db.execute(
                  select(City).where(City.active == True)  # noqa: E712
              )).scalars().all()]
    stats = {"at": now.isoformat(timespec="minutes"), "cities": 0, "days_written": 0, "errors": 0}
    for city in cities:
        try:
            stats["days_written"] += await record_city(db, city, now)
            stats["cities"] += 1
        except Exception as e:
            stats["errors"] += 1
            await db.rollback()
            logger.warning(f"[peaks] {city.name} failed: {e}")
    return stats


async def job_record_peaks() -> None:
    if not getattr(settings, "peaks_enabled", True):
        return
    async with AsyncSessionLocal() as db:
        stats = await record_peaks(db)
    LAST_RUN.clear()
    LAST_RUN.update(stats)
    logger.info(f"[peaks] {stats}")
