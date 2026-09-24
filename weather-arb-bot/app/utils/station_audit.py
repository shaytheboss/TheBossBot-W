"""Check each city's stations against the one Polymarket actually resolves on.

Why this matters. The intraday "official running max" is the higher of the
METAR max from `city.primary_icao` and the Wunderground high scraped from
`city.wunderground_url` — and a lock is only trusted when Wunderground
confirmed it. If either field points at a different station from the one in
the market's resolution rules, the bot confirms locks against the wrong
thermometer. In the settled intraday data, "impossible" NO bets lost with
gaps no rounding explains — Seoul read 31°C and the 26°C bucket won; London
34°C lost with the max 1.1°C past the edge.

Polymarket states the station in each market's rules, normally as a
Wunderground history URL ending in the ICAO code, e.g.
`https://www.wunderground.com/history/daily/gb/london/EGLC`. This module reads
that — first from the description the discovery job already stored on the
market, and only if that was truncated, from the event on Gamma — and compares.

It changes nothing on its own. `apply_fix` is called only from the admin
screen, one city at a time, after the operator has seen the comparison.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from sqlalchemy import desc, select

logger = logging.getLogger(__name__)

# A Wunderground URL, with the station as the last path segment. The optional
# /date/YYYY-M-D tail is dropped: the Wunderground collector appends its own.
_WU_URL = re.compile(
    r"https?://(?:www\.)?wunderground\.com/(?:history/daily|history/airport|weather|dashboard/pws)"
    r"/[^\s\"'<>)]*?/([A-Za-z][A-Za-z0-9]{3})(?:/date/[0-9-]+)?(?=[/\s\"'<>).,]|$)",
    re.IGNORECASE,
)
_ANY_URL = re.compile(r"https?://([^/\s\"'<>)]+)", re.IGNORECASE)
_STATION_NAME = re.compile(
    r"recorded (?:at|by) (?:the )?(.{3,80}?)(?: [Ss]tation| in degrees|,|\.)"
)

VERDICT_OK = "ok"
VERDICT_PRIMARY = "primary_icao_mismatch"
VERDICT_WU = "wunderground_url_mismatch"
VERDICT_BOTH = "both_mismatch"
VERDICT_NOT_WU = "not_wunderground"
VERDICT_UNKNOWN = "unknown"

ACTIONABLE = frozenset({VERDICT_PRIMARY, VERDICT_WU, VERDICT_BOTH})


@dataclass
class StationAudit:
    city_id: int
    city: str
    primary_icao: Optional[str]
    reference_icao: Optional[str]
    wu_url_icao: Optional[str]
    resolution_icao: Optional[str]
    resolution_url: Optional[str]
    resolution_name: Optional[str]
    other_source: Optional[str]
    checked_market: Optional[str]
    verdict: str
    advice: str

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_wu_url(url: str) -> str:
    return re.sub(r"/date/[0-9-]+/?$", "", url.rstrip("/.,)"))


def extract_wu_station(text: Optional[str]) -> Optional[tuple[str, str]]:
    """(ICAO, clean URL) of the first Wunderground station link, or None."""
    if not text:
        return None
    m = _WU_URL.search(text)
    if not m:
        return None
    return m.group(1).upper(), _clean_wu_url(m.group(0))


def extract_station_name(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = _STATION_NAME.search(text)
    return m.group(1).strip() if m else None


def other_source_domain(text: Optional[str]) -> Optional[str]:
    """The first non-Wunderground, non-Polymarket domain named in the rules —
    e.g. the Hong Kong Observatory, which no METAR mirrors."""
    for m in _ANY_URL.finditer(text or ""):
        host = m.group(1).lower()
        if "wunderground" in host or "polymarket" in host:
            continue
        return host
    return None


def _icao_in_url(url: Optional[str]) -> Optional[str]:
    hit = extract_wu_station(url)
    return hit[0] if hit else None


def judge(city, resolution: Optional[tuple[str, str]], name: Optional[str],
          other: Optional[str], checked: Optional[str]) -> StationAudit:
    """Compare one city with the station its market resolves on. Pure."""
    primary = (city.primary_icao or "").upper() or None
    reference = (city.reference_icao or "").upper() or None
    wu_icao = _icao_in_url(city.wunderground_url)
    res_icao, res_url = resolution if resolution else (None, None)

    if res_icao:
        p_ok = primary == res_icao
        w_ok = wu_icao == res_icao
        if p_ok and w_ok:
            verdict, advice = VERDICT_OK, "Matches the Polymarket resolution station."
        elif not p_ok and not w_ok:
            verdict = VERDICT_BOTH
            advice = (f"Polymarket resolves on {res_icao}; METAR reads {primary} and "
                      f"Wunderground reads {wu_icao or 'a URL with no station'}.")
        elif not p_ok:
            verdict = VERDICT_PRIMARY
            advice = f"Polymarket resolves on {res_icao}; METAR reads {primary}."
        else:
            verdict = VERDICT_WU
            advice = (f"Polymarket resolves on {res_icao}; the Wunderground URL points "
                      f"at {wu_icao or 'no station'}.")
    elif other:
        verdict = VERDICT_NOT_WU
        advice = (f"Resolves on {other}, which no METAR mirrors — intraday locks for "
                  f"this city cannot be confirmed. Consider switching intraday off for it.")
    else:
        verdict = VERDICT_UNKNOWN
        advice = "No market with readable resolution rules was found."

    return StationAudit(
        city_id=city.id, city=city.name, primary_icao=primary, reference_icao=reference,
        wu_url_icao=wu_icao, resolution_icao=res_icao, resolution_url=res_url,
        resolution_name=name, other_source=other, checked_market=checked,
        verdict=verdict, advice=advice,
    )


def _texts_from_event(event: Optional[dict]) -> list[str]:
    if not event:
        return []
    out = [event.get("resolutionSource") or "", event.get("description") or ""]
    for m in event.get("markets") or []:
        out += [m.get("resolutionSource") or "", m.get("description") or ""]
    return [t for t in out if t]


async def resolution_for_city(db, city, client, markets_to_check: int = 3):
    """(resolution, name, other, checked_slug) for one city.

    The stored description is tried first — it costs nothing. It is capped at
    500 characters when stored, which can cut the URL off, so Gamma is only
    asked when the stored text has no station in it.
    """
    from app.models.market import Market
    from app.utils.polymarket_discovery import fetch_event_by_slug

    markets = (await db.execute(
        select(Market).where(Market.city_id == city.id)
        .order_by(desc(Market.event_date)).limit(markets_to_check)
    )).scalars().all()

    name = other = None
    for market in markets:
        stored = market.resolution_source or ""
        hit = extract_wu_station(stored)
        name = name or extract_station_name(stored)
        if hit:
            return hit, name, None, market.external_id
        texts = [stored]
        if client is not None and market.external_id:
            try:
                texts += _texts_from_event(await fetch_event_by_slug(client, market.external_id))
            except Exception as e:
                logger.warning(f"[stations] Gamma lookup failed for {market.external_id}: {e}")
        for t in texts:
            hit = extract_wu_station(t)
            name = name or extract_station_name(t)
            if hit:
                return hit, name, None, market.external_id
        other = other or next((d for d in map(other_source_domain, texts) if d), None)
    return None, name, other, (markets[0].external_id if markets else None)


async def audit_cities(db, client, cities: Optional[Iterable] = None) -> list[StationAudit]:
    from app.models.city import City
    if cities is None:
        cities = (await db.execute(select(City).order_by(City.name))).scalars().all()
    out = []
    for city in cities:
        res, name, other, checked = await resolution_for_city(db, city, client)
        out.append(judge(city, res, name, other, checked))
    return out


def apply_fix(city, audit: StationAudit) -> dict:
    """Point the city at the resolution station. Returns {field: (old, new)}.

    The old primary moves to reference_icao rather than being thrown away, so
    it is still collected and the change is easy to reverse from the screen.
    """
    if audit.verdict not in ACTIONABLE or not audit.resolution_icao:
        raise ValueError(f"nothing to apply for verdict {audit.verdict!r}")
    changes: dict = {}
    new_icao = audit.resolution_icao
    old_primary = (city.primary_icao or "").upper()
    if old_primary != new_icao:
        changes["primary_icao"] = (city.primary_icao, new_icao)
        if (city.reference_icao or "").upper() in ("", new_icao):
            changes["reference_icao"] = (city.reference_icao, old_primary or None)
        city.primary_icao = new_icao
        if "reference_icao" in changes:
            city.reference_icao = changes["reference_icao"][1]
    if audit.resolution_url and _icao_in_url(city.wunderground_url) != new_icao:
        changes["wunderground_url"] = (city.wunderground_url, audit.resolution_url)
        city.wunderground_url = audit.resolution_url
    return changes
