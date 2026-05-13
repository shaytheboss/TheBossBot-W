import logging
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector
from app.models.forecast import Forecast

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class GFSCollector(BaseCollector):
    name = "gfs"

    async def collect(self, lat: float, lon: float, model: str = "gfs") -> Optional[dict]:
        wmo_model = "gfs_seamless" if model == "gfs" else "ecmwf_ifs025"
        try:
            resp = await self._get(
                OPEN_METEO_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max",
                    "temperature_unit": "fahrenheit",
                    "windspeed_unit": "kn",
                    "forecast_days": 2,
                    "models": wmo_model,
                    "timezone": "auto",
                },
            )
            data = resp.json()
            daily = data.get("daily", {})
            if not daily or not daily.get("time"):
                return None

            today_str = str(date.today())
            idx = None
            for i, t in enumerate(daily["time"]):
                if t == today_str:
                    idx = i
                    break

            if idx is None:
                return None

            return {
                "predicted_high_f": round(daily["temperature_2m_max"][idx]),
                "predicted_low_f": round(daily["temperature_2m_min"][idx]),
                "wind_max_kt": daily.get("windspeed_10m_max", [None])[idx],
                "model": model,
            }
        except Exception as e:
            logger.error(f"GFS/ECMWF fetch failed ({model}): {e}")
            return None

    async def collect_and_store(
        self,
        city_id: int,
        lat: float,
        lon: float,
        forecast_date: date,
        db: AsyncSession,
        model: str = "gfs",
    ) -> Optional[dict]:
        parsed = await self.collect(lat, lon, model)
        if not parsed:
            return None

        forecast = Forecast(
            city_id=city_id,
            source=model,
            forecast_for_date=forecast_date,
            predicted_high_f=parsed.get("predicted_high_f"),
            predicted_low_f=parsed.get("predicted_low_f"),
            raw_data=parsed,
        )
        db.add(forecast)
        await db.commit()
        logger.info(f"{model.upper()} forecast stored for city {city_id}: {parsed}")
        return parsed
