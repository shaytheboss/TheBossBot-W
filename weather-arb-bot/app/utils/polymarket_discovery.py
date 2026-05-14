"""Polymarket weather market discovery.

Three-source approach (no slug guessing needed):

1. Gamma /markets scan — paginate GET /markets?closed=false, filter by
   temperature keywords in the question field. Each market record contains
   the event slug, so we get the exact slugs Polymarket uses without guessing.

2. CLOB API scan — paginate clob.polymarket.com/markets, same keyword
   filter. The CLOB API is Polymarket's trading API and lists every active
   tradeable market with its token_ids.

3. Slug probing fallback — construct candidate slugs from the known pattern
   and probe with /events?slug=<exact>. Kept as a fast sanity-check.

City matching from found events (3 strategies):
  a. City name substring in event title / slug.
  b. Exact candidate-slug match (from fallback probing).
  c. Wunderground ICAO code in event description.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import date, timedelta
from typing import Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Keywords that identify a temperature / weather market.
TEMP_KEYWORDS = [
    "temperature",
    "highest temp",
    "lowest temp",
    "high temp",
    "daily high",
    "max temp",
]

# City name substrings → polymarket_slug (longest first for safe matching).
CITY_NAMES_TO_SLUG: list[tuple[str, str]] = sorted(
    [
        ("new york city", "nyc"),
        ("new-york-city", "nyc"),
        ("new york", "nyc"),
        ("new-york", "nyc"),
        ("-nyc-", "nyc"),
        ("-nyc ", "nyc"),
        ("los angeles", "los-angeles"),
        ("los-angeles", "los-angeles"),
        ("san francisco", "san-francisco"),
        ("san-francisco", "san-francisco"),
        ("washington dc", "washington-dc"),
        ("washington-dc", "washington-dc"),
        ("las vegas", "las-vegas"),
        ("las-vegas", "las-vegas"),
        ("chicago", "chicago"),
        ("miami", "miami"),
        ("austin", "austin"),
        ("houston", "houston"),
        ("dallas", "dallas"),
        ("denver", "denver"),
        ("seattle", "seattle"),
        ("atlanta", "atlanta"),
        ("phoenix", "phoenix"),
        ("boston", "boston"),
        ("portland", "portland"),
        ("london", "london"),
    ],
    key=lambda x: -len(x[0]),
)

# Slug aliases for slug-probe fallback.
CITY_SLUG_ALIASES: dict[str, list[str]] = {
    "nyc": ["nyc", "new-york", "new-york-city"],
    "los-angeles": ["los-angeles", "la"],
    "san-francisco": ["san-francisco", "sf"],
    "washington-dc": ["washington-dc", "washington"],
    "chicago": ["chicago"],
    "miami": ["miami"],
    "austin": ["austin"],
    "dallas": ["dallas"],
    "houston": ["houston"],
    "denver": ["denver"],
    "seattle": ["seattle"],
    "atlanta": ["atlanta"],
    "phoenix": ["phoenix"],
    "boston": ["boston"],
    "las-vegas": ["las-vegas"],
    "portland": ["portland"],
    "london": ["london"],
}

SLUG_PREFIXES: list[str] = [
    "highest-temperature-in-{city}-on-{month}-{day}-{year}",
    "highest-temperature-in-{city}-on-{month}-{day}",
    "lowest-temperature-in-{city}-on-{month}-{day}-{year}",
    "lowest-temperature-in-{city}-on-{month}-{day}",
]


def city_slug_from_text(text: str) -> Optional[str]:
    """Detect city polymarket_slug from any text (title, question, slug)."""
    t = text.lower()
    for name, slug in CITY_NAMES_TO_SLUG:
        if name in t:
            return slug
    return None


def is_temperature_event(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in TEMP_KEYWORDS)


def candidate_slugs_for_city(city_slug: str, target_date: date) -> list[str]:
    month = calendar.month_name[target_date.month].lower()
    out: list[str] = []
    for alias in CITY_SLUG_ALIASES.get(city_slug, [city_slug]):
        for tpl in SLUG_PREFIXES:
            out.append(tpl.format(
                city=alias, month=month, day=target_date.day, year=target_date.year
            ))
    return out


def build_all_candidates(
    city_slugs: Iterable[str], days_ahead: int
) -> list[tuple[str, str, date]]:
    """Returns list of (city_slug, candidate_slug, target_date)."""
    today = date.today()
    out: list[tuple[str, str, date]] = []
    seen: set[str] = set()
    for d_offset in range(days_ahead + 1):
        target = today + timedelta(days=d_offset)
        for cs in city_slugs:
            for slug in candidate_slugs_for_city(cs, target):
                if slug in seen:
                    continue
                seen.add(slug)
                out.append((cs, slug, target))
    return out


async def fetch_event_by_slug(
    client: httpx.AsyncClient,
    slug: str,
    retries: int = 2,
) -> Optional[dict]:
    """GET /events?slug=<slug> with backoff on 429."""
    for attempt in range(retries + 1):
        try:
            r = await client.get(
                f"{GAMMA_API}/events",
                params={"slug": slug, "limit": 1},
                timeout=20.0,
            )
            if r.status_code == 429:
                wait = 2 ** attempt
                logger.debug(f"fetch_event_by_slug 429 for {slug}, waiting {wait}s")
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data.get("slug"):
                return data
            return None
        except Exception as e:
            logger.debug(f"fetch_event_by_slug({slug}) error: {e}")
            return None
    return None


async def fetch_gamma_temperature_markets(
    client: httpx.AsyncClient, max_pages: int = 15
) -> list[dict]:
    """Paginate Gamma /markets?closed=false and return temperature markets.

    Each record contains: question, slug (event slug), conditionId,
    outcomePrices, tokens, clobTokenIds, etc.
    """
    markets: list[dict] = []
    limit = 200
    for page in range(max_pages):
        offset = page * limit
        try:
            r = await client.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "limit": limit, "offset": offset},
                timeout=30.0,
            )
            if r.status_code == 429:
                await asyncio.sleep(3)
                continue
            if r.status_code != 200:
                logger.warning(f"Gamma /markets page={page}: HTTP {r.status_code}")
                break
            data = r.json()
        except Exception as e:
            logger.warning(f"fetch_gamma_temperature_markets page={page}: {e}")
            break

        if not isinstance(data, list) or not data:
            break

        for m in data:
            q = (m.get("question") or "").lower()
            g = (m.get("groupItemTitle") or "").lower()
            s = (m.get("slug") or "").lower()
            if is_temperature_event(q) or is_temperature_event(g) or is_temperature_event(s):
                markets.append(m)
                logger.info(
                    f"Gamma /markets temp hit: city={city_slug_from_text(q or s)} "
                    f"slug={m.get('slug')} q={m.get('question', '')[:60]}"
                )

        logger.debug(
            f"fetch_gamma_temperature_markets page={page}: {len(data)} records, "
            f"{len(markets)} temp hits total"
        )
        if len(data) < limit:
            break

    logger.info(f"fetch_gamma_temperature_markets: {len(markets)} temperature markets found")
    return markets


async def fetch_clob_temperature_markets(
    client: httpx.AsyncClient, max_pages: int = 20
) -> list[dict]:
    """Paginate CLOB /markets (cursor-based) and return temperature markets.

    The CLOB API is Polymarket's trading endpoint; it lists all active
    tradeable markets with token_ids and prices. No auth required for reading.

    Each record: question, market_slug, condition_id, tokens [{token_id, outcome}],
    active, closed, accepting_orders, outcomePrices, etc.
    """
    markets: list[dict] = []
    next_cursor: Optional[str] = None

    for page in range(max_pages):
        params: dict = {"limit": "500"}
        if next_cursor and next_cursor not in ("", "LTE="):
            params["next_cursor"] = next_cursor

        try:
            r = await client.get(
                f"{CLOB_API}/markets",
                params=params,
                timeout=30.0,
            )
            if r.status_code == 429:
                await asyncio.sleep(3)
                continue
            if r.status_code != 200:
                logger.warning(f"CLOB /markets page={page}: HTTP {r.status_code}")
                break
            data = r.json()
        except Exception as e:
            logger.warning(f"fetch_clob_temperature_markets page={page}: {e}")
            break

        batch = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not batch:
            break

        for m in batch:
            q = (m.get("question") or "").lower()
            s = (m.get("market_slug") or "").lower()
            if is_temperature_event(q) or is_temperature_event(s):
                markets.append(m)
                logger.info(
                    f"CLOB /markets temp hit: city={city_slug_from_text(q or s)} "
                    f"slug={m.get('market_slug')} q={m.get('question', '')[:60]}"
                )

        next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
        logger.debug(
            f"fetch_clob_temperature_markets page={page}: {len(batch)} records, "
            f"{len(markets)} temp hits, cursor={str(next_cursor)[:10]}"
        )
        if not next_cursor or next_cursor in ("", "LTE="):
            break

    logger.info(f"fetch_clob_temperature_markets: {len(markets)} temperature markets found")
    return markets


async def fetch_events_by_tag(
    client: httpx.AsyncClient, tag_slug: str = "weather", limit: int = 200
) -> list[dict]:
    """Query Gamma by tag (weather). Returns event list."""
    out: list[dict] = []
    for params in (
        {"tag_slug": tag_slug, "closed": "false", "limit": limit},
        {"tag": tag_slug, "closed": "false", "limit": limit},
    ):
        try:
            r = await client.get(
                f"{GAMMA_API}/events", params=params, timeout=25.0
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, list) and data:
                out = data
                logger.info(f"fetch_events_by_tag: {len(data)} events params={params}")
                break
        except Exception as e:
            logger.debug(f"fetch_events_by_tag params={params}: {e}")
    return out
