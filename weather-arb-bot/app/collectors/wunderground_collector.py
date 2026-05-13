import logging
import re
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector
from app.models.forecast import Forecast

logger = logging.getLogger(__name__)


class WundergroundCollector(BaseCollector):
    name = "wunderground"

    async def collect(self, url: str) -> Optional[dict]:
        """Scrape the Wunderground page for today's forecast high/low."""
        try:
            resp = await self._get(url)
            soup = BeautifulSoup(resp.text, "lxml")
            return self._parse(soup)
        except Exception as e:
            logger.error(f"Wunderground scrape failed for {url}: {e}")
            return None

    def _parse(self, soup: BeautifulSoup) -> Optional[dict]:
        result: dict = {}

        high_el = soup.select_one("[data-testid='TemperatureValue']")
        if high_el:
            try:
                result["predicted_high_f"] = int(re.sub(r"[^\d-]", "", high_el.text))
            except ValueError:
                pass

        if not result.get("predicted_high_f"):
            for span in soup.find_all("span", class_=re.compile(r"temp|high|forecast", re.I)):
                nums = re.findall(r"\b(\d{2,3})\b", span.text)
                if nums:
                    result["predicted_high_f"] = int(nums[0])
                    break

        cond_el = soup.select_one("[data-testid='wxPhrase']")
        if cond_el:
            result["conditions"] = cond_el.text.strip()

        if not result:
            logger.warning("Wunderground: could not parse any useful data")
            return None

        return result

    async def collect_and_store(
        self, city_id: int, url: str, forecast_date: date, db: AsyncSession
    ) -> Optional[dict]:
        parsed = await self.collect(url)
        if not parsed:
            return None

        forecast = Forecast(
            city_id=city_id,
            source="wunderground",
            forecast_for_date=forecast_date,
            predicted_high_f=parsed.get("predicted_high_f"),
            predicted_low_f=parsed.get("predicted_low_f"),
            conditions=parsed.get("conditions"),
            raw_data=parsed,
        )
        db.add(forecast)
        await db.commit()
        logger.info(f"Wunderground forecast stored for city {city_id}: {parsed}")
        return parsed
