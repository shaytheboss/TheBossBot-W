import logging
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector
from app.models.forecast import Forecast

logger = logging.getLogger(__name__)

TOMORROW_URL = "https://api.tomorrow.io/v4/weather/forecast"


class TomorrowioCollector(BaseCollector):
    name = "tomorrowio"

    def __init__(self, api_key: str = ""):
        super().__init__()
        self.api_key = api_key

    async def collect(
        self, lat: float, lon: float, forecast_date: Optional[date] = None
    ) -> Optional[dict]:
        if not self.api_key:
            return None

        target = forecast_date or date.today()
        target_str = str(target)

        try:
            resp = await self._get(
                TOMORROW_URL,
                params={
                    "location": f"{lat},{lon}",
                    "fields": "temperatureMax,temperatureMin",
                    "timesteps": "1d",
                    "units": "imperial",
                    "apikey": self.api_key,
                },
            )
            data = resp.json()
            daily = (data.get("timelines") or {}).get("daily") or []

            for entry in daily:
                t = (entry.get("time") or "")[:10]
                if t == target_str:
                    vals = entry.get("values") or {}
                    high = vals.get("temperatureMax")
                    low = vals.get("temperatureMin")
                    if high is None:
                        continue
                    # Extract returned coordinates if available
                    location = data.get("location") or {}
                    used_lat = location.get("lat")
                    used_lon = location.get("lon")
                    return {
                        "predicted_high_f": round(high),
                        "predicted_low_f": round(low) if low is not None else None,
                        "model": "tomorrowio",
                        "forecast_date": target_str,
                        "used_lat": used_lat,
                        "used_lon": used_lon,
                    }

            logger.warning(f"Tomorrow.io: {target_str} not found for ({lat},{lon})")
            return None
        except Exception as e:
            logger.error(f"Tomorrow.io fetch failed for {target_str} at ({lat},{lon}): {e}")
            return None

    async def collect_and_store(
        self,
        city_id: int,
        lat: float,
        lon: float,
        forecast_date: date,
        db: AsyncSession,
    ) -> Optional[dict]:
        parsed = await self.collect(lat, lon, forecast_date)
        if not parsed:
            return None

        forecast = Forecast(
            city_id=city_id,
            source="tomorrowio",
            forecast_for_date=forecast_date,
            predicted_high_f=parsed.get("predicted_high_f"),
            predicted_low_f=parsed.get("predicted_low_f"),
            raw_data=parsed,
        )
        db.add(forecast)
        await db.commit()
        logger.info(f"Tomorrow.io stored for city {city_id} date {forecast_date}: {parsed}")
        return parsed
