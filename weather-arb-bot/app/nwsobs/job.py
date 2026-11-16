"""Record weather.gov observations for every active US city. Every 10 minutes.

Per station and run: one GET of the latest LIMIT observations (the
readings are irregular, so twenty span about four hours and a missed run
loses nothing), and an insert of only the timestamps not already stored. About a dozen stations,
so ~70 requests an hour and ~4,000 small rows a day, pruned after
RETENTION_DAYS by the job itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select

from app.collectors.base import BaseCollector
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.nwsobs.models import NwsObservation

logger = logging.getLogger(__name__)

OBS_URL = "https://api.weather.gov/stations/{icao}/observations"
LIMIT = 20
RETENTION_DAYS = 60

LAST_RUN: dict = {}


def is_us_station(icao: Optional[str]) -> bool:
    """api.weather.gov serves only the NWS network: four-letter K stations
    for the cities this bot trades. Paris, Seoul and London answer empty."""
    icao = (icao or "").upper()
    return len(icao) == 4 and icao.startswith("K")


def parse(data: dict) -> list[dict]:
    out = []
    for f in (data or {}).get("features") or []:
        p = f.get("properties") or {}
        ts = p.get("timestamp")
        if not ts:
            continue
        temp = p.get("temperature") or {}
        out.append({
            "observed_at": datetime.fromisoformat(ts.replace("Z", "+00:00")),
            "temperature_c": temp.get("value"),
            "qc": (temp.get("qualityControl") or None),
            "is_metar": bool(p.get("rawMessage")),
        })
    return out


class NwsObservationCollector(BaseCollector):
    name = "nws_obs"

    async def collect(self, icao: str) -> list[dict]:
        resp = await self._get(OBS_URL.format(icao=icao), params={"limit": LIMIT},
                               headers={"Accept": "application/geo+json"})
        return parse(resp.json())


_collector = NwsObservationCollector()


async def record_station(db, icao: str, now: datetime,
                         collector: Optional[NwsObservationCollector] = None) -> int:
    rows = await (collector or _collector).collect(icao)
    if not rows:
        return 0
    oldest = min(r["observed_at"] for r in rows)
    have = {t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
            for t in (await db.execute(
                select(NwsObservation.observed_at).where(
                    NwsObservation.icao == icao, NwsObservation.observed_at >= oldest)
            )).scalars()}
    new = [r for r in rows if r["observed_at"] not in have]
    for r in new:
        db.add(NwsObservation(icao=icao, fetched_at=now, **r))
    if new:
        await db.commit()
    return len(new)


async def record_all(db, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cities = (await db.execute(select(City).where(City.active == True))).scalars().all()
    stations = sorted({c.primary_icao.upper() for c in cities if is_us_station(c.primary_icao)})
    stats = {"at": now.isoformat(timespec="minutes"), "stations": len(stations),
             "stored": 0, "errors": 0}
    for icao in stations:
        try:
            stats["stored"] += await record_station(db, icao, now)
        except Exception as e:   # one station failing must not stop the rest
            stats["errors"] += 1
            logger.warning(f"[nws_obs] {icao}: {type(e).__name__}: {e}")
            await db.rollback()
    if now.hour == 0 and now.minute < 10:
        try:
            await db.execute(delete(NwsObservation).where(
                NwsObservation.observed_at < now - timedelta(days=RETENTION_DAYS)))
            await db.commit()
        except Exception as e:
            logger.warning(f"[nws_obs] prune failed: {e}")
            await db.rollback()
    return stats


async def job_record_nws_observations() -> None:
    if not getattr(settings, "nws_obs_enabled", True):
        return
    async with AsyncSessionLocal() as db:
        stats = await record_all(db)
    LAST_RUN.clear()
    LAST_RUN.update(stats)
    logger.info(f"[nws_obs] {stats}")
