import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from app.collectors.base import BaseCollector
from app.models.market import MarketPrice

logger = logging.getLogger(__name__)

CLOB_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class PolymarketCollector(BaseCollector):
    name = "polymarket"

    async def collect(self, token_id: str, *args, **kwargs) -> Optional[dict]:
        """Fetch current price for a single token."""
        return await self.get_prices([token_id])

    async def get_market(self, market_id: str) -> Optional[dict]:
        try:
            resp = await self._get(f"{GAMMA_API_BASE}/markets/{market_id}")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch market {market_id}: {e}")
            return None

    async def get_prices(self, token_ids: List[str]) -> Optional[dict]:
        if not token_ids:
            return {}
        try:
            resp = await self._get(
                f"{CLOB_API_BASE}/prices",
                params={"token_id": ",".join(token_ids)},
            )
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch CLOB prices: {e}")
            return None

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        try:
            resp = await self._get(f"{CLOB_API_BASE}/book", params={"token_id": token_id})
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            return None

    async def collect_and_store(
        self, outcome_id: int, token_id: str, db: AsyncSession
    ) -> Optional[dict]:
        prices = await self.get_prices([token_id])
        if not prices or token_id not in prices:
            return None

        yes_price = float(prices[token_id])
        no_price = round(1.0 - yes_price, 4)

        now = datetime.now(timezone.utc)
        stmt = (
            insert(MarketPrice)
            .values(
                outcome_id=outcome_id,
                timestamp=now,
                yes_price=yes_price,
                no_price=no_price,
            )
            .on_conflict_do_nothing(constraint="uq_price_outcome_time")
        )
        await db.execute(stmt)
        await db.commit()

        result = {"yes_price": yes_price, "no_price": no_price, "timestamp": now}
        logger.info(f"Polymarket price stored: outcome {outcome_id} = {yes_price}")
        return result
