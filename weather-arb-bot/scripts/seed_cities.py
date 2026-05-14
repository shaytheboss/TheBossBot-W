"""Seed the 12 Polymarket weather cities into the database.

Run once after migrations:
  python -m scripts.seed_cities

Idempotent — matches existing rows by NAME (not ICAO), so primary_icao can be
updated for cities like NYC (KLGA → KNYC).
"""
import asyncio
import logging

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.city import City

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# (name, polymarket_slug, primary_icao, reference_icao, wunderground_url, lat, lon, tz)
CITIES = [
    ("Austin", "austin", "KAUS", "KEDC",
        "https://www.wunderground.com/history/daily/us/tx/austin/KAUS",
        30.1975, -97.6664, "America/Chicago"),
    ("London", "london", "EGLL", "EGKK",
        "https://www.wunderground.com/history/daily/gb/england/heathrow/EGLL",
        51.4775, -0.4614, "Europe/London"),
    ("Dallas", "dallas", "KDFW", "KDAL",
        "https://www.wunderground.com/history/daily/us/tx/dallas/KDFW",
        32.8998, -97.0403, "America/Chicago"),
    ("Chicago", "chicago", "KORD", "KMDW",
        "https://www.wunderground.com/history/daily/us/il/chicago/KORD",
        41.9742, -87.9073, "America/Chicago"),
    ("Denver", "denver", "KDEN", "KAPA",
        "https://www.wunderground.com/history/daily/us/co/denver/KDEN",
        39.8561, -104.6737, "America/Denver"),
    # Polymarket NYC markets resolve via KNYC (Central Park), NOT KLGA.
    ("New York", "nyc", "KNYC", "KLGA",
        "https://www.wunderground.com/history/daily/us/ny/new-york-city/KNYC",
        40.7789, -73.9692, "America/New_York"),
    ("Seattle", "seattle", "KSEA", "KBFI",
        "https://www.wunderground.com/history/daily/us/wa/seattle/KSEA",
        47.4502, -122.3088, "America/Los_Angeles"),
    ("Miami", "miami", "KMIA", "KFLL",
        "https://www.wunderground.com/history/daily/us/fl/miami/KMIA",
        25.7959, -80.2870, "America/New_York"),
    ("San Francisco", "san-francisco", "KSFO", "KOAK",
        "https://www.wunderground.com/history/daily/us/ca/san-francisco/KSFO",
        37.6188, -122.3750, "America/Los_Angeles"),
    ("Los Angeles", "los-angeles", "KLAX", "KVNY",
        "https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX",
        33.9425, -118.4081, "America/Los_Angeles"),
    ("Atlanta", "atlanta", "KATL", "KPDK",
        "https://www.wunderground.com/history/daily/us/ga/atlanta/KATL",
        33.6407, -84.4277, "America/New_York"),
    ("Houston", "houston", "KIAH", "KHOU",
        "https://www.wunderground.com/history/daily/us/tx/houston/KIAH",
        29.9844, -95.3414, "America/Chicago"),
]


async def seed():
    async with AsyncSessionLocal() as db:
        for (name, slug, primary, reference, wu_url, lat, lon, tz) in CITIES:
            # Match by NAME so we can update primary_icao (e.g. NYC: KLGA → KNYC)
            result = await db.execute(select(City).where(City.name == name))
            city = result.scalar_one_or_none()
            if city:
                changes = []
                if city.polymarket_slug != slug:
                    city.polymarket_slug = slug
                    changes.append(f"slug={slug}")
                if city.primary_icao != primary:
                    changes.append(f"icao {city.primary_icao}→{primary}")
                    city.primary_icao = primary
                if city.reference_icao != reference:
                    city.reference_icao = reference
                    changes.append(f"ref={reference}")
                if not city.wunderground_url:
                    city.wunderground_url = wu_url
                    changes.append("wu_url")
                if changes:
                    logger.info(f"Updated {name}: {', '.join(changes)}")
                else:
                    logger.info(f"OK {name} ({primary}) — unchanged")
            else:
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
