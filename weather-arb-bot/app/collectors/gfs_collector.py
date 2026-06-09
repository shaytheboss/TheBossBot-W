import logging
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector
from app.models.forecast import Forecast

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"


class GFSCollector(BaseCollector):
    name = "gfs"

    async def collect(
        self, lat: float, lon: float, model: str = "gfs", forecast_date: Optional[date] = None
    ) -> Optional[dict]:
        target = forecast_date or date.today()
        target_str = str(target)
        days_ahead = max(1, (target - date.today()).days + 2)  # +2 for safety

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
                    "forecast_days": days_ahead,
                    "models": wmo_model,
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
                logger.warning(f"GFS collect: date {target_str} not found in response for {lat},{lon}")
                return None

            return {
                "predicted_high_f": round(daily["temperature_2m_max"][idx]),
                "predicted_low_f": round(daily["temperature_2m_min"][idx]),
                "wind_max_kt": daily.get("windspeed_10m_max", [None])[idx],
                "model": model,
                "forecast_date": target_str,
                # Open-Meteo returns the actual grid point used
                "used_lat": data.get("latitude"),
                "used_lon": data.get("longitude"),
            }
        except Exception as e:
            logger.error(f"GFS/ECMWF fetch failed ({model}) for {target_str}: {e}")
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
        parsed = await self.collect(lat, lon, model, forecast_date)
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
        logger.info(f"{model.upper()} forecast stored for city {city_id} date {forecast_date}: {parsed}")
        return parsed

    async def collect_ensemble(
        self, lat: float, lon: float, forecast_date: Optional[date] = None,
        model: str = "gfs_seamless",
    ) -> Optional[dict]:
        """Fetch ensemble members from Open-Meteo and return daily max/min distribution.

        `model` selects the ensemble system: "gfs_seamless" (default, ~30
        members) or "ecmwf_ifs025" (ECMWF IFS ensemble, ~50 members).
        """
        target = forecast_date or date.today()
        target_str = str(target)
        days_ahead = max(1, (target - date.today()).days + 2)

        try:
            resp = await self._get(
                OPEN_METEO_ENSEMBLE_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "temperature_2m",
                    "models": model,
                    "temperature_unit": "fahrenheit",
                    "forecast_days": days_ahead,
                    "timezone": "auto",
                },
            )
            data = resp.json()
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            if not times:
                return None

            # Collect indices for the target date
            target_indices = [i for i, t in enumerate(times) if t.startswith(target_str)]
            if not target_indices:
                logger.warning(f"Ensemble: date {target_str} not found for {lat},{lon}")
                return None

            # Find all member keys dynamically
            member_keys = sorted(k for k in hourly if k.startswith("temperature_2m_member"))
            if not member_keys:
                return None

            daily_highs = []
            daily_lows = []
            for key in member_keys:
                member_temps = [hourly[key][i] for i in target_indices if hourly[key][i] is not None]
                if member_temps:
                    daily_highs.append(max(member_temps))
                    daily_lows.append(min(member_temps))

            if not daily_highs:
                return None

            daily_highs.sort()
            daily_lows.sort()
            n = len(daily_highs)

            def pct(lst, p):
                return round(lst[int(n * p)])

            return {
                "ensemble_highs": daily_highs,
                "ensemble_lows": daily_lows,
                "ensemble_count": n,
                "mean_high_f": round(sum(daily_highs) / n, 1),
                "mean_low_f": round(sum(daily_lows) / n, 1),
                "p10_high_f": pct(daily_highs, 0.10),
                "p25_high_f": pct(daily_highs, 0.25),
                "p50_high_f": pct(daily_highs, 0.50),
                "p75_high_f": pct(daily_highs, 0.75),
                "p90_high_f": pct(daily_highs, 0.90),
                "p10_low_f": pct(daily_lows, 0.10),
                "p25_low_f": pct(daily_lows, 0.25),
                "p50_low_f": pct(daily_lows, 0.50),
                "p75_low_f": pct(daily_lows, 0.75),
                "p90_low_f": pct(daily_lows, 0.90),
                "forecast_date": target_str,
            }
        except Exception as e:
            logger.error(f"Ensemble fetch failed for {target_str}: {e}")
            return None

    async def collect_ensemble_and_store(
        self,
        city_id: int,
        lat: float,
        lon: float,
        forecast_date: date,
        db: AsyncSession,
        model: str = "gfs_seamless",
        source: str = "gfs_ensemble",
    ) -> Optional[dict]:
        parsed = await self.collect_ensemble(lat, lon, forecast_date, model=model)
        if not parsed:
            return None

        forecast = Forecast(
            city_id=city_id,
            source=source,
            forecast_for_date=forecast_date,
            predicted_high_f=parsed.get("mean_high_f"),
            predicted_low_f=parsed.get("mean_low_f"),
            raw_data=parsed,
        )
        db.add(forecast)
        await db.commit()
        logger.info(
            f"{source} stored for city {city_id} date {forecast_date}: "
            f"n={parsed['ensemble_count']} p50_high={parsed.get('p50_high_f')}"
        )
        return parsed
