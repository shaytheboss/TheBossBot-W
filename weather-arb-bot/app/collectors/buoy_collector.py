import logging
import re
from typing import Optional

from app.collectors.base import BaseCollector

logger = logging.getLogger(__name__)

NDBC_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station_id}.txt"


class BuoyCollector(BaseCollector):
    name = "buoy"

    async def collect(self, station_id: str) -> Optional[dict]:
        """
        Fetch latest NDBC buoy observation.
        Returns sea surface temp (°C), wind direction/speed, and air temp.
        """
        try:
            resp = await self._get(NDBC_URL.format(station_id=station_id.upper()))
            return self._parse(resp.text)
        except Exception as e:
            logger.error(f"Buoy fetch failed for {station_id}: {e}")
            return None

    def _parse(self, text: str) -> Optional[dict]:
        lines = [l for l in text.strip().splitlines() if l and not l.startswith("#")]
        if len(lines) < 2:
            return None

        header_line = None
        for line in text.splitlines():
            if line.startswith("#YY"):
                header_line = line.lstrip("#").split()
                break

        if not header_line:
            header_line = [
                "YY", "MM", "DD", "hh", "mm",
                "WDIR", "WSPD", "GST", "WVHT", "DPD",
                "APD", "MWD", "PRES", "ATMP", "WTMP",
                "DEWP", "VIS", "PTDY", "TIDE",
            ]

        latest = lines[0].split()
        result = {}
        for key, val in zip(header_line, latest):
            if val not in ("MM", "99", "999", "9999", "99.0", "99.00"):
                try:
                    result[key] = float(val)
                except ValueError:
                    result[key] = val

        return {
            "wind_direction": result.get("WDIR"),
            "wind_speed_kt": result.get("WSPD"),
            "air_temp_c": result.get("ATMP"),
            "sea_surface_temp_c": result.get("WTMP"),
            "pressure_hpa": result.get("PRES"),
        }
