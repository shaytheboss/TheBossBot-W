"""Live Polymarket prices for the buckets the study records.

Why not the stored price alone. The price job does poll every in-horizon
bucket every 5 minutes, and the stored value IS Polymarket's midpoint — so it
is normally at most 5 minutes old. Two gaps remain, and both matter to the
study:

  1. No per-row proof of freshness. market_prices is written only when the
     price changes, so a row's timestamp is the last CHANGE, not the last
     check. If the price job stalled for an hour, the study would compare the
     model against a stale price and nothing would say so.
  2. The midpoint is not what you pay. It is the right number for "what does
     the market believe", but "could we have profited" needs the ask.

So each recorded bucket gets one order-book read at snapshot time: bid, ask,
and the mid derived from them. When the book is empty or one-sided the stored
price is used instead, and the row is flagged as such.

Reuse: this calls the existing PolymarketCollector.get_book_summary — the same
read the analyzer uses — on a private instance. Nothing here writes to
market_prices or any other production table.

Cost: one GET per LIVE bucket per hour (dead buckets are never fetched). The
price job and the analyzer already make roughly two CLOB calls per in-horizon
bucket every 5 minutes, so this adds on the order of 2% to Polymarket traffic.
The exact count is reported per run as `book_calls`.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.collectors.polymarket_collector import PolymarketCollector

#: Parallel book reads. Enough to keep an hourly run to seconds, few enough
#: not to look like a burst to the CLOB.
BOOK_CONCURRENCY = 8


def new_collector() -> PolymarketCollector:
    """One collector per run, closed at the end of it. A module-level instance
    would hold an idle connection open for the hour between runs — and keep an
    HTTP client that outlives the context it was created in."""
    return PolymarketCollector()


async def fetch_books(
    token_ids: list[str],
    collector: PolymarketCollector,
    concurrency: int = BOOK_CONCURRENCY,
) -> dict[str, Optional[dict]]:
    """{token_id: {bid, ask, spread, mid} or None}. Never raises: a failed read
    is None, and the caller falls back to the stored price."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(token: str):
        async with sem:
            try:
                return token, await collector.get_book_summary(token)
            except Exception:
                return token, None

    pairs = await asyncio.gather(*(one(t) for t in token_ids if t))
    return dict(pairs)


def price_job_age_min() -> Optional[int]:
    """Minutes since the price job last finished, from the in-process job
    stats. The honest freshness stamp for any row that had to fall back to the
    stored price: small means that price was checked recently even if it has
    not changed; large means the price job is lagging and the row is suspect.
    None right after a restart, before the price job has run once."""
    from app.utils.jobstats import snapshot
    for job in snapshot().get("jobs", []):
        if job.get("job") == "polymarket" and job.get("last_run_s_ago") is not None:
            return int(job["last_run_s_ago"] // 60)
    return None
