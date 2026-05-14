"""Direct slug-based Polymarket weather market discovery.

The Gamma API's `?q=<term>` search does NOT do real text matching for weather
events — it returns trending/unrelated events. The reliable approach is to
construct candidate slugs ourselves and hit `/events?slug=<exact-slug>`
because Polymarket's URL structure for daily weather markets is fixed:

  https://polymarket.com/event/highest-temperature-in-<city>-on-<month>-<day>[-<year>]

We enumerate (city alias) x (date in next N days) x (with/without year) and
fetch each candidate. Hits are ingested.
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

# Per-city aliases used in Polymarket slugs. Anything new we observe goes here.
CITY_SLUG_ALIASES: dict[str, list[str]] = {
    "nyc": ["nyc", "new-york", "new-york-city"],
    "los-angeles": ["la", "los-angeles"],
    "san-francisco": ["sf", "san-francisco"],
    "washington-dc": ["dc", "washington-dc", "washington"],
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

# Slug prefix templates we try for each (city, date) pair.
SLUG_PREFIXES: list[str] = [
    "highest-temperature-in-{city}-on-{month}-{day}-{year}",
    "highest-temperature-in-{city}-on-{month}-{day}",
    "lowest-temperature-in-{city}-on-{month}-{day}-{year}",
    "lowest-temperature-in-{city}-on-{month}-{day}",
]


def aliases_for(city_slug: Optional[str]) -> list[str]:
    if not city_slug:
        return []
    return CITY_SLUG_ALIASES.get(city_slug, [city_slug])


def candidate_slugs_for_city(city_slug: str, target_date: date) -> list[str]:
    month = calendar.month_name[target_date.month].lower()  # "may"
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


async def fetch_event_by_slug(
    client: httpx.AsyncClient, slug: str
) -> Optional[dict]:
    """GET /events?slug=<slug>. Returns the event dict if found, else None.

    A 404 / empty list = not a real event = silently None.
    """
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
    """Fallback: query Gamma by tag (weather).

    Returns events under the weather tag. May still have empty markets[] —
    callers should re-fetch each by slug if they need full data.
    """
    out: list[dict] = []
    # Try tag_slug param first, then related_tags
    for params in (
        {"tag_slug": tag_slug, "closed": "false", "limit": limit},
        {"tag": tag_slug, "closed": "false", "limit": limit},
        {"related_tags": tag_slug, "closed": "false", "limit": limit},
    ):
        try:
            r = await client.get(
                f"{GAMMA_API}/events", params=params, timeout=20.0
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
