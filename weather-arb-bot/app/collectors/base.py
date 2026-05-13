import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; WeatherArbBot/1.0; "
        "+https://github.com/your-repo/weather-arb-bot)"
    )
}


class BaseCollector(ABC):
    """Base class for all data collectors."""

    name: str = "base"
    timeout: int = 30

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        client = await self._get_client()
        try:
            response = await client.get(url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            logger.error(f"[{self.name}] HTTP {e.response.status_code} for {url}")
            raise
        except httpx.RequestError as e:
            logger.error(f"[{self.name}] Request error for {url}: {e}")
            raise

    @abstractmethod
    async def collect(self, *args, **kwargs) -> Any:
        """Collect and return data. Subclasses must implement."""
        ...
