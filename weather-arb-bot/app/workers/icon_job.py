"""DWD ICON forecast fetch job.

Isolated from app/workers/jobs.py so that adding/disabling ICON cannot
regress any of the existing forecast/analyzer jobs. Output is written
to the `forecasts` table with source='icon' and IS consumed by
SignalAggregator (signal key 'icon_forecast') as the 7th deterministic
model in the blend. Disable via settings.icon_enabled=False.
"""
import logging
from datetime import date, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.collectors.icon_collector import IconCollector

logger = logging.getLogger(__name__)

icon_col = IconCollector()

ICON_FORECAST_DAYS_AHEAD = 7


async def job_fetch_icon() -> None:
    if not getattr(settings, "icon_enabled", True):
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        today = date.today()
        dates = [today + timedelta(days=i) for i in range(ICON_FORECAST_DAYS_AHEAD)]
        for city in cities:
            if city.nws_lat is None or city.nws_lon is None:
                continue
            lat, lon = float(city.nws_lat), float(city.nws_lon)
            for d in dates:
                try:
                    await icon_col.collect_and_store(city.id, lat, lon, d, db)
                except Exception as e:
                    logger.error(f"ICON job failed for {city.name} {d}: {e}")
