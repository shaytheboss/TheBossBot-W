import logging
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector
from app.models.forecast import Forecast

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class IconCollector(BaseCollector):
    """DWD ICON forecast via Open-Meteo.

    Stores forecasts with source='icon'. This source IS wired into the
    deterministic blend as the 7th model (_DET_SOURCES in
    probability_estimator). Rows are read back by SignalAggregator under the
    'icon_forecast' signal key.
    """

    name = "icon"

    async def collect(
        self, lat: float, lon: float, forecast_date: Optional[date] = None
    ) -> Optional[dict]:
        target = forecast_date or date.today()
        target_str = str(target)
        days_ahead = max(1, (target - date.today()).days + 2)

        try:
            resp = await self._get(
                OPEN_METEO_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max",
                    "temperature_unit": "fahrenheit",
                    "windspeed_unit": "kn",
                    "forecast_days": days_ahead,
                    "models": "icon_seamless",
                    "timezone": "auto",
                },
            )
            data = resp.json()
            daily = data.get("daily", {})
            if not daily or not daily.get("time"):
                return None

            idx = None
            for i, t in enumerate(daily["time"]):
                if t == target_str:
                    idx = i
                    break

            if idx is None:
                logger.warning(
                    f"ICON collect: date {target_str} not found in response for {lat},{lon}"
                )
                return None

            high = daily["temperature_2m_max"][idx]
            low = daily["temperature_2m_min"][idx]
            if high is None or low is None:
                return None

            return {
                "predicted_high_f": round(high),
                "predicted_low_f": round(low),
                "wind_max_kt": daily.get("windspeed_10m_max", [None])[idx],
                "model": "icon",
                "forecast_date": target_str,
                "used_lat": data.get("latitude"),
                "used_lon": data.get("longitude"),
            }
        except Exception as e:
            logger.error(f"ICON fetch failed for {target_str}: {e}")
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
            source="icon",
            forecast_for_date=forecast_date,
            predicted_high_f=parsed.get("predicted_high_f"),
            predicted_low_f=parsed.get("predicted_low_f"),
            raw_data=parsed,
        )
        db.add(forecast)
        await db.commit()
        logger.info(
            f"ICON forecast stored for city {city_id} date {forecast_date}: {parsed}"
        )
        return parsed
