"""Seed the 12 Polymarket weather cities into the database.

Run once after migrations:
  python -m scripts.seed_cities

Idempotent — skips cities that already exist (matched by primary_icao).

Station sources: Polymarket resolves via Wunderground historical data.
Verify ICAO codes against each market's resolution rules if Polymarket
updates their stations.
"""
import asyncio
import logging

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.city import City

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Each entry: (name, polymarket_slug, primary_icao, reference_icao,
#              wunderground_base_url, nws_lat, nws_lon, timezone)
CITIES = [
    (
        "Austin", "austin",
        "KAUS", "KEDC",
        "https://www.wunderground.com/history/daily/KAUS",
        30.1975, -97.6664, "America/Chicago",
    ),
    (
        "London", "london",
        "EGLL", "EGKK",
        "https://www.wunderground.com/history/daily/EGLL",
        51.4775, -0.4614, "Europe/London",
    ),
    (
        "Dallas", "dallas",
        "KDFW", "KDAL",
        "https://www.wunderground.com/history/daily/KDFW",
        32.8998, -97.0403, "America/Chicago",
    ),
    (
        "Chicago", "chicago",
        "KORD", "KMDW",
        "https://www.wunderground.com/history/daily/KORD",
        41.9742, -87.9073, "America/Chicago",
    ),
    (
        "Denver", "denver",
        "KDEN", "KAPA",
        "https://www.wunderground.com/history/daily/KDEN",
        39.8561, -104.6737, "America/Denver",
    ),
    (
        "New York", "nyc",
        "KNYC", "KJFK",
        "https://www.wunderground.com/history/daily/KNYC",
        40.7789, -73.9692, "America/New_York",
    ),
    (
        "Seattle", "seattle",
        "KSEA", "KBFI",
        "https://www.wunderground.com/history/daily/KSEA",
        47.4502, -122.3088, "America/Los_Angeles",
    ),
    (
        "Miami", "miami",
        "KMIA", "KFLL",
        "https://www.wunderground.com/history/daily/KMIA",
        25.7959, -80.2870, "America/New_York",
    ),
    (
        "San Francisco", "san-francisco",
        "KSFO", "KOAK",
        "https://www.wunderground.com/history/daily/KSFO",
        37.6188, -122.3750, "America/Los_Angeles",
    ),
    (
        "Los Angeles", "los-angeles",
        "KLAX", "KVNY",
        "https://www.wunderground.com/history/daily/KLAX",
        33.9425, -118.4081, "America/Los_Angeles",
    ),
    (
        "Atlanta", "atlanta",
        "KATL", "KPDK",
        "https://www.wunderground.com/history/daily/KATL",
        33.6407, -84.4277, "America/New_York",
    ),
    (
        "Houston", "houston",
        "KIAH", "KHOU",
        "https://www.wunderground.com/history/daily/KIAH",
        29.9844, -95.3414, "America/Chicago",
    ),
]


async def seed():
    async with AsyncSessionLocal() as db:
        for (name, slug, primary, reference, wu_url, lat, lon, tz) in CITIES:
            existing = await db.execute(
                select(City).where(City.primary_icao == primary)
            )
            if existing.scalar_one_or_none():
                logger.info(f"Skipping {name} ({primary}) — already exists")
                continue
            city = City(
                name=name,
                polymarket_slug=slug,
                primary_icao=primary,
                reference_icao=reference,
                wunderground_url=wu_url,
                nws_lat=lat,
                nws_lon=lon,
                timezone=tz,
                active=True,
            )
            db.add(city)
            logger.info(f"Added {name} ({primary})")
        await db.commit()
    logger.info("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
