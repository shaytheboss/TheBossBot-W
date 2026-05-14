"""Direct slug-based + full-scan Polymarket weather market discovery.

Strategy (3 passes):
1. Slug probing: construct candidate slugs from the known Polymarket URL
   pattern and hit /events?slug=<exact> for each.
2. Tag fallback: GET /events?tag_slug=weather to catch events not yet
   in our slug templates.
3. Full open-events scan: paginate GET /events?closed=false and filter by
   temperature keywords — catches any slug format we didn't predict.

City matching from found events:
  a. Exact candidate-slug match (slug_to_city dict).
  b. City name substring in event title or slug.
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

# Per-city aliases used in Polymarket slugs.
CITY_SLUG_ALIASES: dict[str, list[str]] = {
    "nyc": ["nyc", "new-york", "new-york-city", "new-york-ny"],
    "los-angeles": ["los-angeles", "la"],
    "san-francisco": ["san-francisco", "sf"],
    "washington-dc": ["washington-dc", "washington", "dc"],
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
    "las-vegas": ["las-vegas", "vegas"],
    "portland": ["portland"],
    "london": ["london"],
}

# City name substrings -> polymarket_slug (longest first for safe matching).
CITY_NAMES_TO_SLUG: list[tuple[str, str]] = sorted(
    [
        ("new york city", "nyc"),
        ("new-york-city", "nyc"),
        ("new york", "nyc"),
        ("new-york", "nyc"),
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
    key=lambda x: -len(x[0]),  # longest match first
)

# Keywords that identify a temperature market.
TEMP_KEYWORDS = [
    "temperature",
    "highest temp",
    "lowest temp",
    "high temp",
    "will the high",
    "daily high",
    "max temp",
]

# Slug prefix templates we try for each (city, date) pair.
SLUG_PREFIXES: list[str] = [
    "highest-temperature-in-{city}-on-{month}-{day}-{year}",
    "highest-temperature-in-{city}-on-{month}-{day}",
    "lowest-temperature-in-{city}-on-{month}-{day}-{year}",
    "lowest-temperature-in-{city}-on-{month}-{day}",
    "will-the-high-temperature-in-{city}-on-{month}-{day}-exceed",
    "will-the-high-temperature-in-{city}-exceed",
]


def aliases_for(city_slug: Optional[str]) -> list[str]:
    if not city_slug:
        return []
    return CITY_SLUG_ALIASES.get(city_slug, [city_slug])


def candidate_slugs_for_city(city_slug: str, target_date: date) -> list[str]:
    month = calendar.month_name[target_date.month].lower()
    out: list[str] = []
    for alias in aliases_for(city_slug):
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


def city_slug_from_event(event: dict) -> Optional[str]:
    """Detect city from event title or slug using name matching."""
    text = (
        (event.get("title") or "") + " " + (event.get("slug") or "")
    ).lower()
    for name, slug in CITY_NAMES_TO_SLUG:
        if name in text:
            return slug
    return None


def is_temperature_event(event: dict) -> bool:
    """Returns True if event looks like a temperature/weather market."""
    text = (
        (event.get("title") or "") + " " + (event.get("slug") or "")
    ).lower()
    return any(kw in text for kw in TEMP_KEYWORDS)


async def fetch_event_by_slug(
    client: httpx.AsyncClient, slug: str
) -> Optional[dict]:
    """GET /events?slug=<slug>. Returns the event dict if found, else None."""
    try:
        r = await client.get(
            f"{GAMMA_API}/events",
            params={"slug": slug, "limit": 1},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        logger.debug(f"fetch_event_by_slug({slug}) error: {e}")
        return None
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data.get("slug"):
        return data
    return None


async def fetch_events_by_tag(
    client: httpx.AsyncClient, tag_slug: str = "weather", limit: int = 200
) -> list[dict]:
    """Query Gamma by tag (weather). Returns event list."""
    out: list[dict] = []
    for params in (
        {"tag_slug": tag_slug, "closed": "false", "limit": limit},
        {"tag": tag_slug, "closed": "false", "limit": limit},
        {"related_tags": tag_slug, "closed": "false", "limit": limit},
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
                logger.info(
                    f"fetch_events_by_tag: got {len(data)} events using params={params}"
                )
                break
        except Exception as e:
            logger.debug(f"fetch_events_by_tag params={params}: {e}")
    return out


async def fetch_all_open_events(
    client: httpx.AsyncClient, max_pages: int = 15
) -> list[dict]:
    """Paginate through ALL open Polymarket events.

    Used as a last-resort scan to find any temperature markets whose
    slug format we didn't predict in SLUG_PREFIXES.
    """
    events: list[dict] = []
    limit = 200
    for page in range(max_pages):
        offset = page * limit
        try:
            r = await client.get(
                f"{GAMMA_API}/events",
                params={"closed": "false", "limit": limit, "offset": offset},
                timeout=30.0,
            )
            if r.status_code != 200:
                logger.warning(f"fetch_all_open_events page={page}: HTTP {r.status_code}")
                break
            data = r.json()
            if not isinstance(data, list) or not data:
                break
            events.extend(data)
            logger.debug(f"fetch_all_open_events page={page}: +{len(data)} events (total={len(events)})")
            if len(data) < limit:
                break  # last page
        except Exception as e:
            logger.warning(f"fetch_all_open_events page={page} error: {e}")
            break
    logger.info(f"fetch_all_open_events: fetched {len(events)} total open events")
    return events
