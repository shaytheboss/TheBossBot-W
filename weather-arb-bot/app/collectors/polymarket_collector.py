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
        return await self.get_midpoint(token_id)

    async def get_market(self, market_id: str) -> Optional[dict]:
        try:
            resp = await self._get(f"{GAMMA_API_BASE}/markets/{market_id}")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch market {market_id}: {e}")
            return None

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get the midpoint price for a token via CLOB /midpoint endpoint.

        Correct Polymarket CLOB API: GET /midpoint?token_id=X returns {"mid": "0.42"}.
        The /prices endpoint does NOT exist on CLOB — it always returns 400.
        """
        try:
            resp = await self._get(
                f"{CLOB_API_BASE}/midpoint",
                params={"token_id": token_id},
            )
            data = resp.json()
            if isinstance(data, dict) and "mid" in data:
                return float(data["mid"])
            return None
        except Exception as e:
            logger.error(f"Failed to fetch /midpoint for {token_id}: {e}")
            return None

    async def get_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """Get best bid (side=buy) or best ask (side=sell) for a token."""
        try:
            resp = await self._get(
                f"{CLOB_API_BASE}/price",
                params={"token_id": token_id, "side": side},
            )
            data = resp.json()
            if isinstance(data, dict) and "price" in data:
                return float(data["price"])
            return None
        except Exception as e:
            logger.error(f"Failed to fetch /price for {token_id}: {e}")
            return None

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        try:
            resp = await self._get(
                f"{CLOB_API_BASE}/book", params={"token_id": token_id}
            )
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            return None

    async def get_book_summary(self, token_id: str) -> Optional[dict]:
        """Return {bid, ask, spread, mid} from the CLOB orderbook, or None if
        no two-sided market exists. This is the realistic price you'd trade at,
        not the midpoint — essential for low-volume markets where midpoint can
        be far from any executable quote.
        """
        book = await self.get_orderbook(token_id)
        if not book:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = max(float(b["price"]) for b in bids if b.get("price") is not None)
            best_ask = min(float(a["price"]) for a in asks if a.get("price") is not None)
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(f"Malformed book for {token_id}: {e}")
            return None
        if best_bid > best_ask:
            # Crossed book — treat as malformed
            return None
        return {
            "bid": round(best_bid, 4),
            "ask": round(best_ask, 4),
            "spread": round(best_ask - best_bid, 4),
            "mid": round((best_bid + best_ask) / 2, 4),
        }

    async def collect_and_store(
        self, outcome_id: int, token_id: str, db: AsyncSession
    ) -> Optional[dict]:
        yes_price = await self.get_midpoint(token_id)
        if yes_price is None:
            return None

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
