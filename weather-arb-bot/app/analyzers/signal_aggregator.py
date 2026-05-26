import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metar import MetarObservation
from app.models.forecast import Forecast
from app.models.pirep import Pirep
from app.models.market import MarketPrice, MarketOutcome
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)


def _c_bucket_to_f_int_range(
    bmin_c: Optional[int], bmax_c: Optional[int]
) -> tuple[Optional[int], Optional[int]]:
    """Return the integer-°F approximation of a Celsius bucket for display.

    For "32°C" (covers [32, 33)°C = [89.6, 91.4)°F): the integer F readings
    inside are {90, 91}. Returns (90, 91). Used by the formatter to render
    a meaningful "32°C (= 90–91°F)" hint.
    """
    if bmin_c is None and bmax_c is None:
        return None, None
    if bmin_c is not None:
        lo_exact = bmin_c * 9 / 5 + 32
        f_lo = math.ceil(lo_exact)
    else:
        f_lo = None
    if bmax_c is not None:
        hi_exclusive = (bmax_c + 1) * 9 / 5 + 32
        f_hi = math.ceil(hi_exclusive) - 1
    else:
        f_hi = None
    return f_lo, f_hi


class SignalAggregator:
    async def aggregate(
        self, db, city_id, primary_icao, reference_icao, outcome,
        forecast_date: Optional[date] = None,
        is_low_market: bool = False,
        city_lat: Optional[float] = None,
        city_lon: Optional[float] = None,
    ) -> dict:
        signals = {}
        signals["primary_metar"] = await self._latest_metar(db, primary_icao)
        if reference_icao:
            signals["reference_metar"] = await self._latest_metar(db, reference_icao)
        signals["metar_trend"] = await self._metar_trend(db, primary_icao, hours=3)
        signals["wunderground_forecast"] = await self._latest_forecast(db, city_id, "wunderground", forecast_date)
        signals["gfs_forecast"] = await self._latest_forecast(db, city_id, "gfs", forecast_date)
        signals["ecmwf_forecast"] = await self._latest_forecast(db, city_id, "ecmwf", forecast_date)
        signals["hrrr_forecast"] = await self._latest_forecast(db, city_id, "hrrr", forecast_date)
        signals["nws_forecast"] = await self._latest_forecast(db, city_id, "nws", forecast_date)
        signals["tomorrowio_forecast"] = await self._latest_forecast(db, city_id, "tomorrowio", forecast_date)
        signals["meteosource_forecast"] = await self._latest_forecast(db, city_id, "meteosource", forecast_date)
        signals["gfs_ensemble"] = await self._latest_forecast(db, city_id, "gfs_ensemble", forecast_date)
        signals["pireps"] = await self._recent_pireps(db, primary_icao, hours=2)
        signals["market_price"] = await self._latest_price(db, outcome.id)
        signals["price_trend"] = await self._price_trend(db, outcome.id, minutes=60)
        signals["is_low_market"] = is_low_market

        # Defensive: fall back to label inspection when the persisted
        # bucket_unit column hasn't been backfilled by migration 005 yet.
        bucket_unit = resolve_bucket_unit(outcome)
        signals["_bucket_unit"] = bucket_unit
        signals["_bucket_native_min"] = outcome.bucket_min
        signals["_bucket_native_max"] = outcome.bucket_max

        # `_bucket_min`/`_bucket_max` are the F-equivalent integer range, used
        # by the formatter for legacy display logic ("well above 80-81°F").
        if bucket_unit == "C":
            f_lo, f_hi = _c_bucket_to_f_int_range(outcome.bucket_min, outcome.bucket_max)
            signals["_bucket_min"] = f_lo
            signals["_bucket_max"] = f_hi
        else:
            signals["_bucket_min"] = outcome.bucket_min
            signals["_bucket_max"] = outcome.bucket_max

        signals["city_lat"] = city_lat
        signals["city_lon"] = city_lon
        return signals

    async def _latest_metar(self, db, icao):
        result = await db.execute(
            select(MetarObservation).where(MetarObservation.icao == icao)
            .order_by(desc(MetarObservation.observed_at)).limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        return {
            "temperature_f": float(row.temperature_f) if row.temperature_f else None,
            "dew_point_f": float(row.dew_point_f) if row.dew_point_f else None,
            "humidity_pct": row.humidity_pct,
            "wind_direction": row.wind_direction,
            "wind_speed_kt": row.wind_speed_kt,
            "wind_gust_kt": row.wind_gust_kt,
            "pressure_hg": float(row.pressure_hg) if row.pressure_hg else None,
            "observed_at": row.observed_at.isoformat(),
        }

    async def _metar_trend(self, db, icao, hours=3):
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await db.execute(
            select(MetarObservation)
            .where(MetarObservation.icao == icao, MetarObservation.observed_at >= since)
            .order_by(MetarObservation.observed_at)
        )
        rows = result.scalars().all()
        if len(rows) < 2:
            return None
        temps = [float(r.temperature_f) for r in rows if r.temperature_f is not None]
        dews = [float(r.dew_point_f) for r in rows if r.dew_point_f is not None]
        if len(temps) < 2:
            return None
        hours_span = (rows[-1].observed_at - rows[0].observed_at).total_seconds() / 3600
        if hours_span == 0:
            return None
        temp_rate = (temps[-1] - temps[0]) / hours_span
        dew_rate = (dews[-1] - dews[0]) / hours_span if len(dews) >= 2 else None
        return {
            "temp_rate_per_hour": round(temp_rate, 2),
            "dew_rate_per_hour": round(dew_rate, 2) if dew_rate is not None else None,
            "current_temp_f": temps[-1],
            "oldest_temp_f": temps[0],
            "span_hours": round(hours_span, 2),
        }

    async def _latest_forecast(self, db, city_id, source, forecast_date: Optional[date] = None):
        q = select(Forecast).where(Forecast.city_id == city_id, Forecast.source == source)
        if forecast_date is not None:
            q = q.where(Forecast.forecast_for_date == forecast_date)
        q = q.order_by(desc(Forecast.retrieved_at)).limit(1)
        result = await db.execute(q)
        row = result.scalar_one_or_none()
        if not row:
            return None
        out = {
            "predicted_high_f": row.predicted_high_f,
            "predicted_low_f": row.predicted_low_f,
            "conditions": row.conditions,
            "retrieved_at": row.retrieved_at.isoformat(),
        }
        if row.raw_data and isinstance(row.raw_data, dict):
            for k in ("ensemble_highs", "ensemble_lows", "ensemble_count",
                      "mean_high_f", "mean_low_f",
                      "p10_high_f", "p25_high_f", "p50_high_f", "p75_high_f", "p90_high_f",
                      "p10_low_f", "p25_low_f", "p50_low_f", "p75_low_f", "p90_low_f",
                      "used_lat", "used_lon",
                      "grid_id", "grid_x", "grid_y"):
                if k in row.raw_data:
                    out[k] = row.raw_data[k]
        return out

    async def _recent_pireps(self, db, icao, hours=2):
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await db.execute(
            select(Pirep)
            .where(Pirep.near_icao == icao, Pirep.observed_at >= since)
            .order_by(desc(Pirep.observed_at)).limit(20)
        )
        rows = result.scalars().all()
        return [
            {
                "flight_level_ft": r.flight_level_ft,
                "temperature_c": float(r.temperature_c) if r.temperature_c else None,
                "wind_direction": r.wind_direction,
                "wind_speed_kt": r.wind_speed_kt,
                "turbulence": r.turbulence,
                "observed_at": r.observed_at.isoformat(),
            }
            for r in rows
        ]

    async def _latest_price(self, db, outcome_id):
        result = await db.execute(
            select(MarketPrice).where(MarketPrice.outcome_id == outcome_id)
            .order_by(desc(MarketPrice.timestamp)).limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        return {
            "yes_price": float(row.yes_price),
            "no_price": float(row.no_price),
            "timestamp": row.timestamp.isoformat(),
        }

    async def _price_trend(self, db, outcome_id, minutes=60):
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        result = await db.execute(
            select(MarketPrice)
            .where(MarketPrice.outcome_id == outcome_id, MarketPrice.timestamp >= since)
            .order_by(MarketPrice.timestamp)
        )
        rows = result.scalars().all()
        if len(rows) < 2:
            return None
        oldest = float(rows[0].yes_price)
        newest = float(rows[-1].yes_price)
        return {
            "change": round(newest - oldest, 4),
            "oldest_price": oldest,
            "newest_price": newest,
            "num_ticks": len(rows),
        }
