import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from app.collectors.base import BaseCollector
from app.models.metar import MetarObservation
from app.utils.metar_parser import parse_metar_json

logger = logging.getLogger(__name__)

AVWX_METAR_URL = "https://aviationweather.gov/api/data/metar"


class MetarCollector(BaseCollector):
    name = "metar"

    async def collect(self, icao: str) -> Optional[dict]:
        """Fetch latest METAR for an ICAO station."""
        try:
            resp = await self._get(
                AVWX_METAR_URL,
                params={"ids": icao, "format": "json"},
            )
            data = resp.json()
            if not data:
                logger.warning(f"No METAR data returned for {icao}")
                return None
            record = data[0] if isinstance(data, list) else data
            return parse_metar_json(record)
        except Exception as e:
            logger.error(f"Failed to fetch METAR for {icao}: {e}")
            return None

    async def collect_and_store(self, icao: str, db: AsyncSession) -> Optional[dict]:
        """Fetch METAR and upsert into database."""
        parsed = await self.collect(icao)
        if not parsed:
            return None

        stmt = (
            insert(MetarObservation)
            .values(
                icao=icao,
                observed_at=parsed["observed_at"],
                temperature_f=parsed.get("temperature_f"),
                dew_point_f=parsed.get("dew_point_f"),
                humidity_pct=parsed.get("humidity_pct"),
                wind_direction=parsed.get("wind_direction"),
                wind_speed_kt=parsed.get("wind_speed_kt"),
                wind_gust_kt=parsed.get("wind_gust_kt"),
                pressure_hg=parsed.get("pressure_hg"),
                visibility_sm=parsed.get("visibility_sm"),
                conditions=parsed.get("conditions"),
                raw_metar=parsed.get("raw_metar"),
            )
            .on_conflict_do_nothing(constraint="uq_metar_icao_time")
        )
        await db.execute(stmt)
        await db.commit()
        logger.info(f"METAR stored for {icao} at {parsed['observed_at']}")
        return parsed
