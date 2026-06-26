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

# Status codes that are transient and worth retrying (rate-limit or server overload).
# 4xx auth/not-found errors are NOT retried — they are deterministic failures.
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0   # seconds; doubles each attempt: 2s → 4s → 8s


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
        """GET with exponential-backoff retry on transient failures.

        Retries up to _MAX_RETRIES times on:
          - httpx.RequestError  (connection reset, timeout, DNS failure)
          - HTTP 429, 5xx       (rate-limit or server overload)

        Deterministic errors (401, 403, 404, other 4xx) raise immediately.
        """
        client = await self._get_client()
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await client.get(url, **kwargs)
            except httpx.RequestError as e:
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"[{self.name}] Request error (attempt {attempt + 1}/{_MAX_RETRIES + 1})"
                        f" — retrying in {delay:.0f}s: {e}"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"[{self.name}] Request error for {url}: {e}")
                raise

            if response.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                body_hint = response.text[:120].replace("\n", " ")
                logger.warning(
                    f"[{self.name}] HTTP {response.status_code} (attempt {attempt + 1}/{_MAX_RETRIES + 1})"
                    f" — retrying in {delay:.0f}s. Body: {body_hint}"
                )
                await asyncio.sleep(delay)
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                body_hint = e.response.text[:200].replace("\n", " ")
                logger.error(
                    f"[{self.name}] HTTP {e.response.status_code} for {url} — {body_hint}"
                )
                raise
            return response

        # Unreachable: loop always returns or raises before exhausting retries.
        raise RuntimeError(f"[{self.name}] _get exhausted retries for {url}")  # pragma: no cover

    @abstractmethod
    async def collect(self, *args, **kwargs) -> Any:
        """Collect and return data. Subclasses must implement."""
        ...
