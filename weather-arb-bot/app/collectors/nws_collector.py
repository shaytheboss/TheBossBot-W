import logging
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector
from app.models.forecast import Forecast

logger = logging.getLogger(__name__)

NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"


class NWSCollector(BaseCollector):
    name = "nws"

    async def _get_forecast_url(self, lat: float, lon: float) -> tuple:
        """Resolve NWS grid forecast URL from coordinates.

        Returns (forecast_url, grid_info_dict) where grid_info_dict may contain
        used_lat, used_lon, and grid identifiers from the NWS points response.
        """
        try:
            resp = await self._get(NWS_POINTS_URL.format(lat=lat, lon=lon))
            data = resp.json()
            props = data.get("properties") or {}
            forecast_url = props.get("forecast")
            grid_info: dict = {}
            # NWS returns the precise grid center point coordinates
            relative_location = props.get("relativeLocation") or {}
            rl_geom = relative_location.get("geometry") or {}
            rl_coords = rl_geom.get("coordinates")
            if rl_coords and len(rl_coords) >= 2:
                grid_info["used_lon"] = rl_coords[0]
                grid_info["used_lat"] = rl_coords[1]
            # Also store grid office / x / y identifiers
            grid_info["grid_id"] = props.get("gridId")
            grid_info["grid_x"] = props.get("gridX")
            grid_info["grid_y"] = props.get("gridY")
            return forecast_url, grid_info
        except Exception as e:
            logger.error(f"NWS points lookup failed for ({lat},{lon}): {e}")
            return None, {}

    async def collect(
        self, lat: float, lon: float, forecast_date: Optional[date] = None
    ) -> Optional[dict]:
        """Fetch NWS forecast and return the specified date's high/low.

        forecast_date is honoured: we search the NWS periods list for the
        matching calendar date instead of always defaulting to today.
        """
        target = forecast_date or date.today()
        target_str = str(target)

        forecast_url, grid_info = await self._get_forecast_url(lat, lon)
        if not forecast_url:
            return None
        try:
            resp = await self._get(forecast_url)
            data = resp.json()
            periods = data["properties"]["periods"]
            result: dict = {"raw_periods": periods}

            # Include grid / coordinate info returned by the NWS points API
            result.update(grid_info)

            for period in periods:
                # startTime looks like "2024-05-17T06:00:00-05:00"; first 10 chars = date
                start = (period.get("startTime") or "")[:10]
                if start == target_str:
                    if period.get("isDaytime"):
                        result["predicted_high_f"] = period.get("temperature")
                        result["conditions"] = period.get("shortForecast")
                    else:
                        result["predicted_low_f"] = period.get("temperature")
                    if "predicted_high_f" in result and "predicted_low_f" in result:
                        break

            if "predicted_high_f" not in result and "predicted_low_f" not in result:
                logger.warning(
                    f"NWS: no period found for {target_str} at ({lat},{lon})"
                )
                return None

            return result
        except Exception as e:
            logger.error(f"NWS forecast fetch failed: {e}")
            return None

    async def collect_and_store(
        self, city_id: int, lat: float, lon: float, forecast_date: date, db: AsyncSession
    ) -> Optional[dict]:
        parsed = await self.collect(lat, lon, forecast_date)
        if not parsed:
            return None

        forecast = Forecast(
            city_id=city_id,
            source="nws",
            forecast_for_date=forecast_date,
            predicted_high_f=parsed.get("predicted_high_f"),
            predicted_low_f=parsed.get("predicted_low_f"),
            conditions=parsed.get("conditions"),
            raw_data={"periods": parsed.get("raw_periods", [])[:3]},
        )
        db.add(forecast)
        await db.commit()
        logger.info(f"NWS stored for city {city_id} date {forecast_date}")
        return parsed
