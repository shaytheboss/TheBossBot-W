"""Collect the nearby stations' METARs. Every 10 minutes, one request.

Frugal by construction:
  - only cities with a market for the city's local today that is not yet
    resolved — no market, nothing to decide, nothing fetched;
  - only between WINDOW_START and WINDOW_END local time, where the daily
    high is decided (the morning rise tells nothing the forecast does not);
  - all due stations in one aviationweather.gov request per CHUNK ids
    (one request for the whole fleet in practice), with `hours` covering a
    missed run;
  - rows go into metar_observations with the bot's own conflict rule, so a
    reading already stored is never written twice.

About 120 stations around 32 settlement stations; at most ~10 hours a day
each, one or two METARs an hour: a few thousand small rows a day.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.collectors.base import BaseCollector
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.market import Market
from app.models.metar import MetarObservation
from app.nearby.stations import neighbours
from app.utils.metar_parser import parse_metar_json

logger = logging.getLogger(__name__)

METAR_URL = "https://aviationweather.gov/api/data/metar"
WINDOW_START = 10      # local hour
WINDOW_END = 20        # local hour, exclusive
CHUNK = 80             # ids per request
HOURS = 2              # history per request: a missed run loses nothing

LAST_RUN: dict = {}


def in_window(now: datetime, tz_name: Optional[str]) -> Optional[date]:
    """The city's local date when its local clock is inside the window."""
    local = now.astimezone(ZoneInfo(tz_name or "UTC"))
    return local.date() if WINDOW_START <= local.hour < WINDOW_END else None


async def due_stations(db, now: datetime) -> dict[str, str]:
    """{neighbour icao: city name} for every city that has something to decide now."""
    cities = (await db.execute(select(City).where(City.active == True))).scalars().all()
    due: dict[str, str] = {}
    for city in cities:
        nb = neighbours(city.primary_icao)
        if not nb:
            continue
        local_day = in_window(now, city.timezone)
        if local_day is None:
            continue
        open_market = (await db.execute(
            select(Market.id).where(Market.city_id == city.id, Market.event_date == local_day,
                                    Market.resolved == False).limit(1)
        )).first()
        if open_market is None:
            continue
        for icao in nb:
            due.setdefault(icao, city.name)
    return due


class NearbyMetarCollector(BaseCollector):
    name = "nearby_metar"

    async def collect(self, icaos: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(icaos), CHUNK):
            resp = await self._get(METAR_URL, params={
                "ids": ",".join(icaos[i:i + CHUNK]), "format": "json", "hours": HOURS})
            data = resp.json() if resp.content else []
            out.extend(r for r in (data or []) if isinstance(r, dict) and r.get("icaoId"))
        return out


_collector = NearbyMetarCollector()


async def store(db, records: list[dict]) -> int:
    stored = 0
    for rec in records:
        p = parse_metar_json(rec)
        if p.get("temperature_f") is None:
            continue
        res = await db.execute(
            insert(MetarObservation).values(
                icao=rec["icaoId"].upper(), observed_at=p["observed_at"],
                temperature_f=p.get("temperature_f"), dew_point_f=p.get("dew_point_f"),
                humidity_pct=p.get("humidity_pct"), wind_direction=p.get("wind_direction"),
                wind_speed_kt=p.get("wind_speed_kt"), wind_gust_kt=p.get("wind_gust_kt"),
                pressure_hg=p.get("pressure_hg"), visibility_sm=p.get("visibility_sm"),
                conditions=p.get("conditions"), raw_metar=p.get("raw_metar"),
            ).on_conflict_do_nothing(constraint="uq_metar_icao_time")
        )
        stored += int(res.rowcount or 0)
    await db.commit()
    return stored


async def record_nearby(db, now: Optional[datetime] = None,
                        collector: Optional[NearbyMetarCollector] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    due = await due_stations(db, now)
    stats = {"at": now.isoformat(timespec="minutes"), "cities": len(set(due.values())),
             "stations": len(due), "readings": 0, "stored": 0, "error": None}
    if not due:
        return stats
    try:
        records = await (collector or _collector).collect(sorted(due))
        stats["readings"] = len(records)
        stats["stored"] = await store(db, records)
    except Exception as e:   # research only: a failure here must never reach trading
        stats["error"] = f"{type(e).__name__}: {e}"
        logger.warning(f"[nearby] {stats['error']}")
        await db.rollback()
    return stats


async def job_record_nearby() -> None:
    if not getattr(settings, "nearby_obs_enabled", True):
        return
    async with AsyncSessionLocal() as db:
        stats = await record_nearby(db)
    LAST_RUN.clear()
    LAST_RUN.update(stats)
    logger.info(f"[nearby] {stats}")
