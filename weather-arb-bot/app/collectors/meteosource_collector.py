import logging
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector
from app.models.forecast import Forecast

logger = logging.getLogger(__name__)

METEOSOURCE_URL = "https://www.meteosource.com/api/v1/free/point"


class MeteosourceCollector(BaseCollector):
    name = "meteosource"

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
                METEOSOURCE_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "sections": "daily",
                    "units": "us",
                    "key": self.api_key,
                },
            )
            data = resp.json()
            daily_data = (data.get("daily") or {}).get("data") or []

            for entry in daily_data:
                day = entry.get("day") or ""
                if day == target_str:
                    all_day = entry.get("all_day") or {}
                    high = all_day.get("temperature_max")
                    low = all_day.get("temperature_min")
                    if high is None:
                        # Some responses nest differently
                        high = entry.get("temperature_max")
                        low = entry.get("temperature_min")
                    if high is None:
                        continue
                    # Extract returned coordinates if available in the response
                    used_lat = data.get("lat")
                    used_lon = data.get("lon")
                    return {
                        "predicted_high_f": round(high),
                        "predicted_low_f": round(low) if low is not None else None,
                        "model": "meteosource",
                        "forecast_date": target_str,
                        "used_lat": used_lat,
                        "used_lon": used_lon,
                    }

            logger.warning(f"Meteosource: {target_str} not found for ({lat},{lon})")
            return None
        except Exception as e:
            logger.error(f"Meteosource fetch failed for {target_str} at ({lat},{lon}): {e}")
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
            source="meteosource",
            forecast_for_date=forecast_date,
            predicted_high_f=parsed.get("predicted_high_f"),
            predicted_low_f=parsed.get("predicted_low_f"),
            raw_data=parsed,
        )
        db.add(forecast)
        await db.commit()
        logger.info(f"Meteosource stored for city {city_id} date {forecast_date}: {parsed}")
        return parsed
