"""Tomorrow.io forecast fetch job — budget-aware, market-priority.

Why this exists: the Tomorrow.io free tier allows only ~25 requests/hour and
500/day. The old shared external-forecast job fired 48 cities x 3 dates = 144
calls in one burst every 4h, so the first handful of cities succeeded and every
other city was rate-limited forever (collector_miss showed ~25 miss-days for
all cities except one).

Strategy here:
  - Runs hourly with a hard per-run request budget (default 20 → 480/day,
    inside both the 25/h and 500/day caps).
  - Cities with OPEN unresolved markets are fetched first — those are the only
    forecasts that can affect a trade.
  - Remaining budget is spent on other active cities via a rotating cursor so
    every city still gets refreshed over the course of a day.

Isolated in its own module (like icon_job) so changes here cannot regress the
Meteosource path that still lives in job_fetch_external_forecasts.
"""
import logging
from datetime import date, timedelta
from typing import List, Sequence, Tuple

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.market import Market

logger = logging.getLogger(__name__)

TOMORROWIO_FORECAST_DAYS = 3   # today + 2 ahead, matches EXTERNAL_FORECAST_DAYS

# Rotating cursor over the non-market cities. Module-level state: resets on
# restart, which only means the rotation starts from the top again — harmless.
_rotation_cursor: int = 0


def select_cities_for_budget(
    market_city_ids: Sequence[int],
    all_city_ids: Sequence[int],
    cursor: int,
    budget_requests: int,
    requests_per_city: int,
) -> Tuple[List[int], int]:
    """Pick which cities to fetch this run within the request budget.

    Priority 1: cities with open markets (in given order) — always first.
    Priority 2: remaining cities, starting at `cursor`, wrapping around.

    Returns (city_ids_to_fetch, new_cursor). Pure function for testability.
    """
    if requests_per_city <= 0 or budget_requests <= 0:
        return [], cursor

    max_cities = budget_requests // requests_per_city
    if max_cities <= 0:
        return [], cursor

    picked: List[int] = []
    seen = set()
    for cid in market_city_ids:
        if len(picked) >= max_cities:
            break
        if cid not in seen:
            picked.append(cid)
            seen.add(cid)

    rest = [cid for cid in all_city_ids if cid not in seen]
    new_cursor = cursor
    if rest and len(picked) < max_cities:
        n = len(rest)
        start = cursor % n
        idx = start
        while len(picked) < max_cities and len(seen) < len(market_city_ids) + n:
            cid = rest[idx % n]
            if cid not in seen:
                picked.append(cid)
                seen.add(cid)
            idx += 1
            if (idx - start) >= n:
                break
        new_cursor = idx % n

    return picked, new_cursor


async def job_fetch_tomorrowio() -> None:
    """Fetch Tomorrow.io forecasts under the free-tier request budget."""
    from app.workers.jobs import tomorrowio_col  # shared collector instance

    if not tomorrowio_col.api_key:
        return

    global _rotation_cursor
    budget = int(getattr(settings, "tomorrowio_requests_per_run", 20))

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = [c for c in result.scalars().all()
                  if c.nws_lat is not None and c.nws_lon is not None]
        by_id = {c.id: c for c in cities}

        # Cities with open markets, soonest event first — these matter most.
        mk = await db.execute(
            select(Market.city_id, Market.event_date)
            .where(Market.resolved == False)
            .order_by(Market.event_date)
        )
        market_city_ids: List[int] = []
        for cid, _ev in mk.all():
            if cid in by_id and cid not in market_city_ids:
                market_city_ids.append(cid)

        picked_ids, _rotation_cursor = select_cities_for_budget(
            market_city_ids=market_city_ids,
            all_city_ids=[c.id for c in cities],
            cursor=_rotation_cursor,
            budget_requests=budget,
            requests_per_city=TOMORROWIO_FORECAST_DAYS,
        )
        if not picked_ids:
            return

        today = date.today()
        dates = [today + timedelta(days=i) for i in range(TOMORROWIO_FORECAST_DAYS)]
        ok = 0
        for cid in picked_ids:
            city = by_id[cid]
            lat, lon = float(city.nws_lat), float(city.nws_lon)
            for d in dates:
                try:
                    if await tomorrowio_col.collect_and_store(city.id, lat, lon, d, db):
                        ok += 1
                except Exception as e:
                    logger.error(f"Tomorrow.io job failed for {city.name} {d}: {e}")

        logger.info(
            f"job_fetch_tomorrowio: {len(picked_ids)} cities "
            f"({len(market_city_ids)} with open markets), {ok} forecasts stored, "
            f"budget={budget} req"
        )
