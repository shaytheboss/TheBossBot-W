import asyncio
import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.metar import MetarObservation
from app.models.opportunity import Opportunity
from app.collectors.metar_collector import MetarCollector
from app.collectors.wunderground_collector import WundergroundCollector
from app.collectors.nws_collector import NWSCollector
from app.collectors.gfs_collector import GFSCollector
from app.collectors.hrrr_collector import HRRRCollector
from app.collectors.tomorrowio_collector import TomorrowioCollector
from app.collectors.meteosource_collector import MeteosourceCollector
from app.collectors.pirep_collector import PirepCollector
from app.collectors.polymarket_collector import PolymarketCollector
from app.analyzers.opportunity_detector import detect_opportunities
from app.bot.telegram_bot import send_opportunity_alert, send_side_alert
from app.utils.units import resolve_bucket_unit, temp_in_bucket
from app.utils.polymarket_discovery import (
    build_all_candidates,
    extract_event_slug,
    fetch_event_by_slug,
    fetch_events_by_tag,
    fetch_gamma_temperature_markets,
    fetch_clob_temperature_markets,
    city_slug_from_text,
)

logger = logging.getLogger(__name__)

metar_col = MetarCollector()
wunder_col = WundergroundCollector()
nws_col = NWSCollector()
gfs_col = GFSCollector()
hrrr_col = HRRRCollector()
tomorrowio_col = TomorrowioCollector(api_key=settings.tomorrowio_api_key)
meteosource_col = MeteosourceCollector(api_key=settings.meteosource_api_key)
pirep_col = PirepCollector()
poly_col = PolymarketCollector()

FORECAST_DAYS_AHEAD = 7
EXTERNAL_FORECAST_DAYS = 3
DISCOVERY_CONCURRENCY = 3

POLYMARKET_ICAO_TO_CITY_SLUG = {
    "KNYC": "nyc", "KLGA": "nyc", "KJFK": "nyc",
    "KLAX": "los-angeles", "KMIA": "miami", "KDEN": "denver",
    "KORD": "chicago", "KAUS": "austin",
    "KIAH": "houston", "KHOU": "houston",
    "KDFW": "dallas", "KPHL": "philadelphia",
    "KATL": "atlanta", "KSEA": "seattle",
    "KSFO": "san-francisco", "KDCA": "washington-dc",
    "KPHX": "phoenix", "KBOS": "boston",
    "KLAS": "las-vegas", "KPDX": "portland",
    "EGLL": "london",
}

WUNDERGROUND_ICAO_RX = re.compile(
    r"wunderground\.com/[\w/\-]+/([A-Z]{4})(?=[/\s\)\.\,]|$)", re.IGNORECASE
)

_MONTH_BY_NAME = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH_BY_NAME.update({
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
})
_MONTHS_RX = "|".join(sorted(_MONTH_BY_NAME, key=len, reverse=True))

_DATE_PROSE_RX = re.compile(
    rf"(?:on|for)\s+({_MONTHS_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:[,\s]+(\d{{4}}))?" ,
    re.IGNORECASE,
)
_DATE_SLUG_RX = re.compile(
    rf"({_MONTHS_RX})-(\d{{1,2}})(?:-(\d{{4}}))?", re.IGNORECASE,
)

SKIP_MARKET_KEYWORDS = ("lowest", "daily low", "low temperature", "minimum temp")

LAST_DISCOVERY: dict = {
    "started_at": None,
    "finished_at": None,
    "candidates_tried": 0,
    "events_found": 0,
    "markets_ingested": 0,
    "markets_refreshed": 0,
    "hits": [],
    "errors": [],
}

LAST_RETRO_FIX: dict = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "markets_total": 0,
    "markets_checked": 0,
    "markets_skipped_no_poly": 0,
    "markets_skipped_no_token": 0,
    "opportunities_corrected": 0,
    "corrections": [],
    "error": None,
}


def _is_skippable_market(text: str) -> bool:
    if not text:
        return False
    lo = text.lower()
    return any(kw in lo for kw in SKIP_MARKET_KEYWORDS)


def _parse_date(text: str) -> Optional[date]:
    if not text:
        return None
    for rx in (_DATE_PROSE_RX, _DATE_SLUG_RX):
        m = rx.search(text)
        if not m:
            continue
        month_name, day_str, year_str = m.group(1), m.group(2), m.group(3)
        month = _MONTH_BY_NAME.get(month_name.lower())
        if not month:
            continue
        try:
            day = int(day_str)
            year = int(year_str) if year_str else date.today().year
            parsed = date(year, month, day)
            # Year rollover: a "january-2" slug discovered in late December has
            # no explicit year and would default to the CURRENT year — ~12
            # months in the past — and get discarded. Markets are never listed
            # more than a few weeks out, so a far-past date with an assumed
            # year means the event is actually next year.
            if not year_str and parsed < date.today() - timedelta(days=180):
                parsed = date(year + 1, month, day)
            return parsed
        except ValueError:
            continue
    return None


def _extract_icao_from_description(desc: str) -> Optional[str]:
    if not desc:
        return None
    m = WUNDERGROUND_ICAO_RX.search(desc)
    return m.group(1).upper() if m else None


def _is_celsius_bucket(label: str) -> bool:
    lo = label.lower()
    if "°c" in lo or "celsius" in lo:
        return True
    if re.search(r"\d\s*c(?:\s|$|or\b|/)", lo):
        return True
    return False


def _parse_temp_range_unit(text: str, is_c: bool) -> tuple[Optional[int], Optional[int]]:
    """Parse a bucket label to (bmin, bmax) in NATIVE unit.

    For Celsius: single-value labels like "32°C" return (32, 32) so the
    resolution range [bmin, bmax+1) = [32, 33)°C is exactly 1 degree wide.
    For Fahrenheit single-value labels we keep the legacy (v, v+1) behaviour
    so existing F data — which was stored that way — continues to resolve
    consistently.
    """
    if not text:
        return None, None
    t = text.lower().replace("°", "").strip()

    m = re.search(r"(\d+)\s*(?:-|to|–)\s*(\d+)\s*[fc]?", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"(\d+)\s*\+", t)
    if m:
        return int(m.group(1)), None

    m = re.search(r"(\d+)\s*[fc]?\s*(?:or\s+)?(?:above|higher|over|more|greater)", t)
    if m:
        return int(m.group(1)), None

    m = re.search(r"(?:above|over|greater\s+than)\s+(\d+)", t)
    if m:
        return int(m.group(1)), None

    m = re.search(r"(\d+)\s*[fc]?\s*(?:or\s+)?(?:below|lower|under|less)", t)
    if m:
        return None, int(m.group(1))

    m = re.search(r"(?:below|under|less\s+than)\s+(\d+)", t)
    if m:
        return None, int(m.group(1))

    nums = re.findall(r"\d+", t)
    if nums:
        v = int(nums[0])
        if is_c:
            return v, v
        return v, v + 1

    return None, None


def _parse_bucket(label: str) -> tuple[Optional[int], Optional[int], str]:
    """Return (bucket_min, bucket_max, bucket_unit) in NATIVE units.

    bucket_unit is 'C' or 'F'. Native means no F↔C conversion is performed
    — the values are integers expressed in whichever unit the label uses.
    """
    is_c = _is_celsius_bucket(label)
    bmin, bmax = _parse_temp_range_unit(label, is_c)
    return bmin, bmax, ("C" if is_c else "F")


async def _refresh_outcome_bounds(db: AsyncSession, market: Market, raw_markets: list) -> int:
    existing_outcomes_q = await db.execute(
        select(MarketOutcome).where(MarketOutcome.market_id == market.id)
    )
    by_label = {o.bucket_label: o for o in existing_outcomes_q.scalars().all()}

    updated = 0
    for m in raw_markets or []:
        gtitle = (m.get("groupItemTitle") or "").strip()
        question = (m.get("question") or "").strip()
        bucket_label = (gtitle or question[:50] or "unknown")[:100]

        target = by_label.get(bucket_label)
        if target is None:
            continue

        bmin, bmax, unit = _parse_bucket(gtitle)
        if bmin is None and bmax is None:
            bmin, bmax, unit = _parse_bucket(question)

        if (
            target.bucket_min != bmin
            or target.bucket_max != bmax
            or (getattr(target, "bucket_unit", "F") or "F") != unit
        ):
            logger.info(
                f"REFRESH outcome id={target.id} label={bucket_label!r} "
                f"({target.bucket_min},{target.bucket_max},{getattr(target, 'bucket_unit', 'F')}) "
                f"→ ({bmin},{bmax},{unit})"
            )
            target.bucket_min = bmin
            target.bucket_max = bmax
            target.bucket_unit = unit
            updated += 1

    if updated:
        await db.commit()
    return updated


async def _ingest_event(event: dict, city: City, db: AsyncSession) -> tuple[int, int]:
    slug = event.get("slug")
    if not slug:
        return 0, 0

    title = (event.get("title") or "")
    if _is_skippable_market(title) or _is_skippable_market(slug):
        logger.debug(f"Skipping non-highest market: {slug}")
        return 0, 0

    existing = await db.execute(select(Market).where(Market.external_id == slug))
    existing_market = existing.scalar_one_or_none()
    if existing_market is not None:
        refreshed = await _refresh_outcome_bounds(db, existing_market, event.get("markets") or [])
        return 0, refreshed

    target_date = _parse_date(slug) or _parse_date(title)
    if not target_date:
        logger.debug(f"No date parsed from event slug={slug}")
        return 0, 0
    today = date.today()
    if target_date < today or target_date > today + timedelta(days=FORECAST_DAYS_AHEAD):
        return 0, 0

    end_str = event.get("endDate") or event.get("end_date_iso")
    resolution_time: Optional[datetime] = None
    if end_str:
        try:
            resolution_time = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
        except ValueError:
            pass

    market = Market(
        city_id=city.id,
        external_id=slug,
        platform="polymarket",
        question=title or f"Highest temp in {city.name} on {target_date}",
        event_date=target_date,
        resolution_time=resolution_time,
        resolution_source=(event.get("description") or "")[:500] or None,
    )
    db.add(market)
    await db.flush()

    count = 0
    for m in event.get("markets", []) or []:
        gtitle = (m.get("groupItemTitle") or "").strip()
        question = (m.get("question") or "").strip()
        bucket_label = gtitle or question[:50] or "unknown"

        bucket_min, bucket_max, bucket_unit = _parse_bucket(gtitle)
        if bucket_min is None and bucket_max is None:
            bucket_min, bucket_max, bucket_unit = _parse_bucket(question)

        token_id: Optional[str] = None
        for token in m.get("tokens", []) or []:
            if str(token.get("outcome", "")).lower() == "yes":
                token_id = token.get("tokenId") or token.get("token_id")
                break
        if not token_id:
            ids = m.get("clobTokenIds") or []
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except json.JSONDecodeError:
                    ids = []
            token_id = ids[0] if ids else None

        outcome = MarketOutcome(
            market_id=market.id,
            bucket_label=bucket_label[:100],
            bucket_min=bucket_min,
            bucket_max=bucket_max,
            bucket_unit=bucket_unit,
            token_id=token_id,
        )
        db.add(outcome)
        count += 1

    await db.commit()
    logger.info(f"DISCOVERY HIT ✓ city={city.name} date={target_date} slug={slug} outcomes={count}")
    return count, 0


def _city_for_event(
    event: dict,
    slug_to_city: dict[str, City],
    cities_by_pm_slug: dict[str, City],
) -> Optional[City]:
    slug = (event.get("slug") or "").lower()
    if slug in slug_to_city:
        return slug_to_city[slug]

    title = event.get("title") or ""
    pm_slug = city_slug_from_text(title + " " + slug)
    if pm_slug and pm_slug in cities_by_pm_slug:
        return cities_by_pm_slug[pm_slug]

    description = event.get("description") or ""
    icao = _extract_icao_from_description(description)
    if not icao:
        for m in event.get("markets", []) or []:
            icao = _extract_icao_from_description(m.get("description") or "")
            if icao:
                break
    if icao:
        city_pm_slug = POLYMARKET_ICAO_TO_CITY_SLUG.get(icao)
        if city_pm_slug and city_pm_slug in cities_by_pm_slug:
            return cities_by_pm_slug[city_pm_slug]

    return None


async def _notify_telegram(text: str) -> None:
    if not settings.telegram_bot_token:
        return
    try:
        from telegram import Bot
        from app.models.alert import TelegramUser
        async with AsyncSessionLocal() as db:
            users = (await db.execute(select(TelegramUser))).scalars().all()
        bot = Bot(token=settings.telegram_bot_token)
        for user in users:
            try:
                await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.debug(f"_notify_telegram send to {user.chat_id} failed: {e}")
    except Exception as e:
        logger.warning(f"_notify_telegram error: {e}")


async def job_discover_markets(notify: bool = True) -> dict:
    started = datetime.now(timezone.utc)
    LAST_DISCOVERY.update({
        "started_at": started.isoformat(),
        "finished_at": None,
        "candidates_tried": 0,
        "events_found": 0,
        "markets_ingested": 0,
        "markets_refreshed": 0,
        "hits": [],
        "errors": [],
    })

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(City).where(City.active == True, City.polymarket_slug != None)
        )
        cities = list(result.scalars().all())

    if not cities:
        msg = "job_discover_markets: no active cities with polymarket_slug"
        logger.warning(msg)
        LAST_DISCOVERY["errors"].append(msg)
        LAST_DISCOVERY["finished_at"] = datetime.now(timezone.utc).isoformat()
        return dict(LAST_DISCOVERY)

    city_slugs = [c.polymarket_slug for c in cities]
    candidates = build_all_candidates(city_slugs, FORECAST_DAYS_AHEAD)
    LAST_DISCOVERY["candidates_tried"] = len(candidates)

    slug_to_city: dict[str, City] = {}
    for city_slug, slug, _target in candidates:
        slug_to_city[slug.lower()] = next(c for c in cities if c.polymarket_slug == city_slug)
    cities_by_pm_slug: dict[str, City] = {c.polymarket_slug: c for c in cities}

    found_events: list[dict] = []
    seen_slugs: set[str] = set()

    def _add_event(ev: dict) -> None:
        ev_slug = (ev.get("slug") or "").lower()
        if ev_slug and ev_slug not in seen_slugs:
            seen_slugs.add(ev_slug)
            found_events.append(ev)

    async with httpx.AsyncClient(
        headers={"User-Agent": "weather-arb-bot/1.0"}, timeout=30.0
    ) as client:

        gamma_temp_markets = await fetch_gamma_temperature_markets(client, max_pages=15)
        gamma_event_slugs: set[str] = set()
        for gm in gamma_temp_markets:
            ev_slug = extract_event_slug(gm)
            if ev_slug:
                gamma_event_slugs.add(ev_slug)
        logger.info(
            f"DISCOVERY pass1 (Gamma /markets): {len(gamma_temp_markets)} temp markets, "
            f"{len(gamma_event_slugs)} unique event slugs"
        )
        for ev_slug in sorted(gamma_event_slugs):
            if ev_slug in seen_slugs:
                continue
            event = await fetch_event_by_slug(client, ev_slug)
            if event:
                _add_event(event)
            await asyncio.sleep(0.2)
        pass1_count = len(found_events)

        clob_temp_markets = await fetch_clob_temperature_markets(client, max_pages=20)
        clob_event_slugs: set[str] = set()
        for cm in clob_temp_markets:
            ev_slug = extract_event_slug(cm)
            if ev_slug:
                clob_event_slugs.add(ev_slug)
        logger.info(
            f"DISCOVERY pass2 (CLOB /markets): {len(clob_temp_markets)} temp markets, "
            f"{len(clob_event_slugs)} candidate event slugs"
        )
        for ev_slug in sorted(clob_event_slugs - gamma_event_slugs):
            if ev_slug in seen_slugs:
                continue
            event = await fetch_event_by_slug(client, ev_slug)
            if event:
                _add_event(event)
            await asyncio.sleep(0.2)
        pass2_count = len(found_events) - pass1_count

        sem = asyncio.Semaphore(DISCOVERY_CONCURRENCY)

        async def try_slug(slug: str) -> None:
            async with sem:
                event = await fetch_event_by_slug(client, slug)
                if event:
                    _add_event(event)

        await asyncio.gather(*(try_slug(s) for _c, s, _d in candidates))
        pass3_count = len(found_events) - pass1_count - pass2_count

        tag_events = await fetch_events_by_tag(client, "weather", limit=200)
        for ev in tag_events:
            ev_slug = (ev.get("slug") or "").lower()
            if ev_slug and ev_slug not in seen_slugs:
                if not ev.get("markets"):
                    full = await fetch_event_by_slug(client, ev_slug)
                    if full:
                        ev = full
                _add_event(ev)
        pass4_count = len(found_events) - pass1_count - pass2_count - pass3_count

        logger.info(
            f"DISCOVERY: pass1={pass1_count} pass2={pass2_count} pass3={pass3_count} pass4={pass4_count} "
            f"| Gamma temp={len(gamma_temp_markets)} CLOB temp={len(clob_temp_markets)}"
        )

    LAST_DISCOVERY["events_found"] = len(found_events)

    already_tracked = 0
    unmatched_samples: list[str] = []
    async with AsyncSessionLocal() as db:
        for event in found_events:
            try:
                city = _city_for_event(event, slug_to_city, cities_by_pm_slug)
                if not city:
                    if len(unmatched_samples) < 5:
                        unmatched_samples.append(f"{event.get('slug', '?')} ({event.get('title', '?')[:40]})")
                    continue
                added, refreshed = await _ingest_event(event, city, db)
                if added > 0:
                    LAST_DISCOVERY["markets_ingested"] += added
                    LAST_DISCOVERY["hits"].append({"slug": event.get("slug"), "city": city.name, "outcomes": added})
                else:
                    already_tracked += 1
                if refreshed > 0:
                    LAST_DISCOVERY["markets_refreshed"] += refreshed
            except Exception as e:
                msg = f"ingest failed for slug={event.get('slug')}: {e}"
                logger.error(msg, exc_info=True)
                LAST_DISCOVERY["errors"].append(msg)

    if unmatched_samples:
        logger.warning(
            f"DISCOVERY: {len(found_events) - len(LAST_DISCOVERY['hits']) - already_tracked} unmatched events. "
            f"Samples: {unmatched_samples}"
        )

    LAST_DISCOVERY["finished_at"] = datetime.now(timezone.utc).isoformat()

    if notify:
        if LAST_DISCOVERY["markets_ingested"] > 0:
            city_names = ", ".join(h["city"] for h in LAST_DISCOVERY["hits"])
            msg = (
                f"\U0001f50d *Discovery*: ingested *{LAST_DISCOVERY['markets_ingested']}* new outcomes "
                f"({city_names})."
            )
            if LAST_DISCOVERY["markets_refreshed"]:
                msg += f" Refreshed {LAST_DISCOVERY['markets_refreshed']} existing outcome bounds."
        else:
            refreshed_note = (
                f" (refreshed {LAST_DISCOVERY['markets_refreshed']} bounds)"
                if LAST_DISCOVERY["markets_refreshed"] else ""
            )
            msg = (
                f"\U0001f50d *Discovery*: {len(found_events)} events, 0 new "
                f"(already tracked: {already_tracked}){refreshed_note}. "
                f"Gamma temp markets: {len(gamma_temp_markets)}, CLOB: {len(clob_temp_markets)}."
            )
        await _notify_telegram(msg)

    return dict(LAST_DISCOVERY)


async def job_fetch_metars():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        for city in cities:
            try:
                await metar_col.collect_and_store(city.primary_icao, db)
                if city.reference_icao:
                    await metar_col.collect_and_store(city.reference_icao, db)
            except Exception as e:
                logger.error(f"METAR job failed for city {city.name}: {e}")


async def job_fetch_wunderground():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        today = date.today()
        for city in cities:
            try:
                await wunder_col.collect_and_store(city.id, city.wunderground_url, today, db)
            except Exception as e:
                logger.error(f"Wunderground job failed for {city.name}: {e}")


async def job_fetch_nws():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        today = date.today()
        dates = [today + timedelta(days=i) for i in range(FORECAST_DAYS_AHEAD)]
        for city in cities:
            if city.nws_lat is None or city.nws_lon is None:
                continue
            for d in dates:
                try:
                    await nws_col.collect_and_store(
                        city.id, float(city.nws_lat), float(city.nws_lon), d, db
                    )
                except Exception as e:
                    logger.error(f"NWS job failed for {city.name} {d}: {e}")


async def job_fetch_models():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        today = date.today()
        dates = [today + timedelta(days=i) for i in range(FORECAST_DAYS_AHEAD)]
        for city in cities:
            if city.nws_lat is None or city.nws_lon is None:
                continue
            lat, lon = float(city.nws_lat), float(city.nws_lon)
            for d in dates:
                for model in ("gfs", "ecmwf"):
                    try:
                        await gfs_col.collect_and_store(city.id, lat, lon, d, db, model)
                    except Exception as e:
                        logger.error(f"{model} job failed for {city.name} {d}: {e}")
                try:
                    await hrrr_col.collect_and_store(city.id, lat, lon, d, db)
                except Exception as e:
                    logger.error(f"HRRR job failed for {city.name} {d}: {e}")
                try:
                    await gfs_col.collect_ensemble_and_store(city.id, lat, lon, d, db)
                except Exception as e:
                    logger.error(f"GFS ensemble job failed for {city.name} {d}: {e}")
                try:
                    await gfs_col.collect_ensemble_and_store(
                        city.id, lat, lon, d, db,
                        model="ecmwf_ifs025", source="ecmwf_ensemble",
                    )
                except Exception as e:
                    logger.error(f"ECMWF ensemble job failed for {city.name} {d}: {e}")


async def job_fetch_external_forecasts():
    if not tomorrowio_col.api_key and not meteosource_col.api_key:
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        today = date.today()
        dates = [today + timedelta(days=i) for i in range(EXTERNAL_FORECAST_DAYS)]
        for city in cities:
            if city.nws_lat is None or city.nws_lon is None:
                continue
            lat, lon = float(city.nws_lat), float(city.nws_lon)
            for d in dates:
                if tomorrowio_col.api_key:
                    try:
                        await tomorrowio_col.collect_and_store(city.id, lat, lon, d, db)
                    except Exception as e:
                        logger.error(f"Tomorrow.io job failed for {city.name} {d}: {e}")
                if meteosource_col.api_key:
                    try:
                        await meteosource_col.collect_and_store(city.id, lat, lon, d, db)
                    except Exception as e:
                        logger.error(f"Meteosource job failed for {city.name} {d}: {e}")


async def job_fetch_pireps():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        for city in cities:
            try:
                await pirep_col.collect_and_store(city.primary_icao, db)
            except Exception as e:
                logger.error(f"PIREP job failed for {city.name}: {e}")


async def job_fetch_polymarket():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MarketOutcome)
            .join(Market)
            .where(Market.resolved == False, MarketOutcome.token_id != None)
        )
        outcomes = result.scalars().all()
        if not outcomes:
            logger.debug("job_fetch_polymarket: no outcomes with token_id")
            return
        logger.info(f"job_fetch_polymarket: fetching prices for {len(outcomes)} outcomes")
        for outcome in outcomes:
            try:
                await poly_col.collect_and_store(outcome.id, outcome.token_id, db)
            except Exception as e:
                logger.error(f"Polymarket job failed for outcome {outcome.id}: {e}")


async def job_run_intraday():
    """Intraday (same-day, hours-scale) detection cycle. Fully isolated from
    the daily analyzer — see INTRADAY.md."""
    from app.intraday.detector import detect_intraday
    from app.bot.telegram_bot import send_intraday_alert, send_intraday_realert, send_basket_alert
    async with AsyncSessionLocal() as db:
        try:
            opportunities, realerts, baskets = await detect_intraday(db)
            if opportunities:
                logger.info(f"job_run_intraday: {len(opportunities)} intraday opportunities")
            if baskets:
                logger.info(f"job_run_intraday: {len(baskets)} basket plays detected")
            for opp in opportunities:
                try:
                    await send_intraday_alert(opp, db)
                except Exception as e:
                    logger.error(f"Failed to send intraday alert for {opp.id}: {e}")
            for ra in realerts:
                try:
                    await send_intraday_realert(ra, db)
                except Exception as e:
                    logger.error(f"Failed to send intraday realert: {e}")
            for basket in baskets:
                try:
                    await send_basket_alert(basket, db)
                except Exception as e:
                    logger.error(f"Failed to send basket alert for {basket.get('basket_id')}: {e}")
        except Exception as e:
            logger.error(f"Intraday job failed: {e}", exc_info=True)


async def job_update_model_skill():
    """עדכון מאגר דיוק-המודלים הפר-עירוני (model_skill).

    רץ גם כ-job תקופתי וגם מיד אחרי שכל settlement מסתיים, כדי שתוצאה
    טרייה מפולימרקט תשפיע על המשקולות עוד באותו יום ולא בסבב הבא.
    """
    from app.analyzers.model_skill import update_model_skill
    async with AsyncSessionLocal() as db:
        try:
            summary = await update_model_skill(db)
            logger.info(f"job_update_model_skill: {summary}")
        except Exception as e:
            logger.error(f"job_update_model_skill failed: {e}", exc_info=True)


async def job_run_analyzer():
    async with AsyncSessionLocal() as db:
        try:
            opportunities, side_alerts = await detect_opportunities(db)
            if opportunities:
                logger.info(f"job_run_analyzer: {len(opportunities)} opportunities found")
            if side_alerts:
                logger.info(f"job_run_analyzer: {len(side_alerts)} side alerts")
            for opp in opportunities:
                try:
                    await send_opportunity_alert(opp, db)
                except Exception as e:
                    logger.error(f"Failed to send alert for opportunity {opp.id}: {e}")
            for alert in side_alerts:
                try:
                    await send_side_alert(alert, db)
                except Exception as e:
                    logger.error(f"Failed to send side alert ({alert.get('type')}): {e}")
        except Exception as e:
            logger.error(f"Analyzer job failed: {e}", exc_info=True)

    # Beta estimator runs in its own session so a beta failure can never affect alpha.
    async with AsyncSessionLocal() as db:
        try:
            from app.analyzers.beta_opportunity_detector import detect_beta_opportunities
            from app.bot.telegram_bot import send_beta_opportunity_alert
            beta_opps = await detect_beta_opportunities(db)
            if beta_opps:
                logger.info(f"job_run_analyzer [beta]: {len(beta_opps)} beta opportunities found")
            for opp in beta_opps:
                try:
                    await send_beta_opportunity_alert(opp, db)
                except Exception as e:
                    logger.error(f"[beta] Failed to send alert for opportunity {opp.id}: {e}")
        except Exception as e:
            logger.error(f"Beta analyzer job failed (non-critical): {e}", exc_info=True)


async def _fetch_polymarket_winning_outcomes(
    event_slug: str,
    outcomes: list,
) -> tuple[frozenset, bool, Optional[str]]:
    """Query Polymarket Gamma API to find which bucket's YES actually resolved.

    Returns (winning_outcome_ids, poly_resolved, note_str).
    winning_outcome_ids is the set of MarketOutcome.id records that Polymarket
    says won (i.e. their YES token resolved to 1.0). poly_resolved=True is
    only returned when a winning bucket was both found AND matched to a DB
    outcome — partial resolutions (loser buckets settled early, winner still
    open) and unmatched winners report False so callers retry later instead
    of settling positions against an empty winner set.
    """
    token_id_to_outcome_id: dict[str, int] = {
        o.token_id: o.id for o in outcomes if o.token_id
    }
    label_to_outcome_id: dict[str, int] = {
        o.bucket_label.strip().lower(): o.id for o in outcomes
    }

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "weather-arb-bot/1.0"}, timeout=20.0
        ) as client:
            # Resolved/closed events are NOT returned by default from the Gamma
            # API. Try several parameter combinations to find the event whether
            # it is still open, recently closed, or fully resolved.
            event: Optional[dict] = None
            for extra in [
                {"closed": "true"},   # resolved events
                {"active": "false"},  # inactive (settled) events
                {},                   # fallback: active-only (unlikely to be resolved, but kept)
            ]:
                try:
                    r = await client.get(
                        "https://gamma-api.polymarket.com/events",
                        params={"slug": event_slug, "limit": 1, **extra},
                        timeout=20.0,
                    )
                except Exception as _req_err:
                    logger.debug(f"_fetch_polymarket_winning_outcomes request error ({extra}): {_req_err}")
                    continue
                if r.status_code == 429:
                    await asyncio.sleep(3)
                    continue
                if r.status_code != 200:
                    logger.debug(
                        f"_fetch_polymarket_winning_outcomes: HTTP {r.status_code} for {event_slug} params={extra}"
                    )
                    continue
                data = r.json()
                candidate = (
                    data[0] if isinstance(data, list) and data
                    else (data if isinstance(data, dict) and data.get("slug") else None)
                )
                if candidate and candidate.get("markets"):
                    event = candidate
                    logger.debug(
                        f"_fetch_polymarket_winning_outcomes: found event {event_slug} with params={extra}"
                    )
                    break

            if not event:
                logger.info(
                    f"_fetch_polymarket_winning_outcomes: no event found for slug={event_slug}"
                )
                return frozenset(), False, None

            sub_markets = event.get("markets") or []

            # Polymarket Gamma API quirk: outcomes, outcomePrices and
            # clobTokenIds all arrive as JSON-encoded strings, not native
            # arrays — e.g. outcomePrices = "[\"1\", \"0\"]". Must json.loads()
            # before indexing, or indexing the raw string yields a character.
            def _decode_json_field(raw) -> list:
                if isinstance(raw, str):
                    try:
                        v = json.loads(raw)
                        return v if isinstance(v, list) else []
                    except (json.JSONDecodeError, ValueError):
                        return []
                if isinstance(raw, (list, tuple)):
                    return list(raw)
                return []

            # A sub-market is resolved if Polymarket says so, OR (defensively)
            # if it's closed and its prices are a definitive 1/0 split.
            def _is_resolved(m: dict, prices: list) -> bool:
                if m.get("resolved") is True:
                    return True
                if m.get("closed") is True and prices:
                    try:
                        vals = sorted(float(p) for p in prices)
                        return vals[0] <= 0.01 and vals[-1] >= 0.99
                    except (ValueError, TypeError):
                        return False
                return False

            # Locate the "Yes" index from the per-market outcomes labels.
            # CRITICAL: never assume index 0 == Yes. Polymarket orders
            # outcomePrices / clobTokenIds to match the outcomes array, which
            # may be ["No", "Yes"]. Indexing [0] blindly mis-reads the Yes
            # price for those markets and flips the winner. When no "Yes"
            # label exists at all (malformed/missing outcomes array) we return
            # None and let the explicit tokens[] fallback below decide —
            # guessing index 0 could read the No price as Yes.
            def _yes_index(outcomes_arr: list) -> Optional[int]:
                for i, lbl in enumerate(outcomes_arr):
                    if str(lbl).strip().lower() == "yes":
                        return i
                return None

            any_resolved = False
            resolved_flags: list = []
            for m in sub_markets:
                prices_chk = _decode_json_field(m.get("outcomePrices"))
                rflag = _is_resolved(m, prices_chk)
                resolved_flags.append(rflag)
                if rflag:
                    any_resolved = True
            if not any_resolved:
                return frozenset(), False, None

            winning_outcome_ids: set[int] = set()
            notes: list[str] = []

            for m, is_res in zip(sub_markets, resolved_flags):
                if not is_res:
                    continue

                outcomes_arr = _decode_json_field(m.get("outcomes"))
                prices_arr = _decode_json_field(m.get("outcomePrices"))
                tokens_arr = _decode_json_field(m.get("clobTokenIds"))
                yes_idx = _yes_index(outcomes_arr)

                # Determine if YES won using the correctly-located Yes price.
                yes_won = False
                if yes_idx is not None and yes_idx < len(prices_arr):
                    try:
                        yes_won = float(prices_arr[yes_idx]) >= 0.99
                    except (ValueError, TypeError):
                        pass
                # CLOB-shaped fallback: tokens[] with explicit winner / price.
                if not yes_won:
                    for tok in (m.get("tokens") or []):
                        if str(tok.get("outcome", "")).lower() == "yes":
                            if tok.get("winner") is True:
                                yes_won = True
                            else:
                                try:
                                    yes_won = float(tok.get("price")) >= 0.99
                                except (ValueError, TypeError):
                                    pass
                            break

                bucket_label = (
                    m.get("groupItemTitle") or m.get("question") or "?"
                ).strip()

                if not yes_won:
                    continue

                # Match the winning bucket back to our MarketOutcome record.
                # Strategy 1: YES token_id (located via the same yes_idx).
                yes_token: Optional[str] = None
                if yes_idx is not None and yes_idx < len(tokens_arr) and tokens_arr[yes_idx]:
                    yes_token = str(tokens_arr[yes_idx])
                if not yes_token:
                    for tok in (m.get("tokens") or []):
                        if str(tok.get("outcome", "")).lower() == "yes":
                            yes_token = (
                                tok.get("tokenId") or tok.get("token_id")
                                or tok.get("id")
                            )
                            yes_token = str(yes_token) if yes_token else None
                            break

                matched_id: Optional[int] = None
                if yes_token and yes_token in token_id_to_outcome_id:
                    matched_id = token_id_to_outcome_id[yes_token]

                # Strategy 2: match by bucket label (case-insensitive).
                if matched_id is None:
                    matched_id = label_to_outcome_id.get(bucket_label.lower())

                if matched_id is not None:
                    winning_outcome_ids.add(matched_id)
                    notes.append(f"{bucket_label} YES=1.0")
                    logger.info(
                        f"Polymarket winner: event={event_slug} bucket={bucket_label!r} "
                        f"outcome_id={matched_id} token={yes_token}"
                    )
                else:
                    logger.warning(
                        f"Polymarket winner found but no DB match: event={event_slug} "
                        f"bucket={bucket_label!r} token={yes_token}"
                    )
                    notes.append(f"{bucket_label} YES=1.0 (unmatched)")

            note = "Polymarket: " + (", ".join(notes) if notes else "resolved (no match)")

            # SAFETY: never report "resolved" without an identified winning
            # bucket. Polymarket sometimes resolves sub-markets at different
            # times (impossible buckets settle early, the winner later) — and
            # a winner may also fail to match a DB outcome. Settling positions
            # against an EMPTY winner set would mark every YES as LOSS and
            # every NO as WIN while the market is not actually decided. Treat
            # such states as "not yet resolved" and retry on the next run.
            if not winning_outcome_ids:
                logger.info(
                    f"_fetch_polymarket_winning_outcomes: {event_slug} has resolved "
                    f"sub-markets but no identified winning bucket — deferring ({note})"
                )
                return frozenset(), False, note

            return frozenset(winning_outcome_ids), True, note

    except Exception as e:
        logger.warning(f"_fetch_polymarket_winning_outcomes({event_slug}): {e}")
        return frozenset(), False, None


def _mark_outcome_winners(outcomes: list, winning_outcome_ids: set) -> None:
    """Persist Polymarket's verdict on each bucket onto MarketOutcome.won.

    True for the winning bucket(s), False for every other bucket on the same
    (now-resolved) market. Called from every resolution path so the dashboard
    has an authoritative, model-independent record of which bucket won — which
    is what per-model accuracy scoring compares each forecast against.
    """
    for o in outcomes:
        o.won = o.id in winning_outcome_ids


async def _send_resolution_alert(
    city: City, market: Market, actual_high_f: Optional[float], opps: list,
    winning_outcome_ids: set, db, outcomes: list, resolution_source: str = "METAR",
) -> None:
    from app.models.alert import TelegramUser
    from telegram import Bot

    if not settings.telegram_bot_token:
        return

    wins = [o for o in opps if o.outcome == "WIN"]
    losses = [o for o in opps if o.outcome == "LOSS"]
    if not wins and not losses:
        return

    if len(wins) > len(losses):
        header = "✅ *Resolution WIN*"
    elif len(losses) > len(wins):
        header = "❌ *Resolution LOSS*"
    else:
        header = "\U0001f91d *Resolution PUSH*"

    poly_url = f"https://polymarket.com/event/{market.external_id}"

    has_c_bucket = any(resolve_bucket_unit(o) == "C" for o in outcomes)

    def _metar_str() -> Optional[str]:
        if actual_high_f is None:
            return None
        if has_c_bucket:
            actual_c = (actual_high_f - 32.0) * 5.0 / 9.0
            return f"{actual_high_f}°F / {actual_c:.1f}°C"
        return f"{actual_high_f}°F"

    # When Polymarket is the authoritative source, the WIN/LOSS rows reflect the
    # bucket Polymarket actually settled. The METAR high can disagree (different
    # station, rounding, time window) so showing it as "Actual high" previously
    # contradicted the rows — e.g. METAR 86°F displayed while the 84-85°F bucket
    # was the real winner. Show the winning bucket as the result and demote the
    # METAR reading to a clearly-labelled reference line.
    winning_labels = [
        o.bucket_label for o in outcomes if o.id in winning_outcome_ids
    ]
    result_lines: list[str]
    if resolution_source == "Polymarket" and winning_labels:
        result_lines = [f"\U0001f3c6 Winning bucket: *{', '.join(winning_labels)}*"]
        metar = _metar_str()
        if metar is not None:
            result_lines.append(f"\U0001f321️ METAR high (reference only): {metar}")
    else:
        metar = _metar_str()
        actual_str = f"*{metar}*" if metar is not None else "*N/A (METAR unavailable)*"
        result_lines = [f"\U0001f321️ Actual high: {actual_str}"]

    lines = [
        header,
        f"\U0001f4cd {city.name} (`{city.primary_icao}`) — {market.event_date.strftime('%b %d, %Y')}",
        *result_lines,
        f"\U0001f4ca Resolved via: {resolution_source}",
        f"[Polymarket]({poly_url})",
        "",
    ]
    total_pnl = 0.0
    wins_n = 0
    losses_n = 0
    for opp in opps:
        oc_res = await db.execute(select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id))
        oc = oc_res.scalar_one_or_none()
        label = oc.bucket_label if oc else f"outcome #{opp.outcome_id}"
        emoji = "✅" if opp.outcome == "WIN" else "❌"

        if opp.virtual_entry_price is not None:
            entry_cents = round(float(opp.virtual_entry_price) * 100)
        else:
            yes_price = float(opp.market_price)
            side_price = (1.0 - yes_price) if opp.side == "NO" else yes_price
            entry_cents = round(side_price * 100)

        line = f"{emoji} {label[:35]} {opp.side} @ {entry_cents}¢ → {opp.outcome}"

        if opp.virtual_pnl is not None and opp.virtual_shares:
            cost = float(opp.virtual_cost or 0.0)
            payout = float(opp.virtual_payout or 0.0)
            pnl = float(opp.virtual_pnl)
            total_pnl += pnl
            if opp.outcome == "WIN":
                wins_n += 1
            elif opp.outcome == "LOSS":
                losses_n += 1
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            line += (
                f" | {opp.virtual_shares} sh × {entry_cents}¢ = ${cost:.2f} cost "
                f"→ payout ${payout:.2f} → {pnl_str} P&L"
            )
        lines.append(line)

    if wins_n + losses_n > 0:
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        lines.append("")
        lines.append(f"\U0001f4ca Day P&L: {pnl_str} ({wins_n} wins, {losses_n} losses)")

    text = "\n".join(lines)
    users_result = await db.execute(select(TelegramUser))
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        if user.cities_watched and city.id not in user.cities_watched:
            continue
        try:
            await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send resolution alert to {user.chat_id}: {e}")


async def _settle_intraday_for_market(db, market, winning_outcome_ids: set) -> list:
    """Settle intraday positions for a resolved market.

    Separate from (and called after) the daily settlement so an intraday
    failure can never affect the daily flow. Returns the settled rows.
    """
    from app.models.intraday import IntradayOpportunity
    opps_result = await db.execute(
        select(IntradayOpportunity)
        .join(MarketOutcome, MarketOutcome.id == IntradayOpportunity.outcome_id)
        .where(
            MarketOutcome.market_id == market.id,
            IntradayOpportunity.outcome == None,
        )
    )
    iopps = opps_result.scalars().all()
    now = datetime.now(timezone.utc)
    for opp in iopps:
        bucket_won = opp.outcome_id in winning_outcome_ids
        if opp.side == "YES":
            opp.outcome = "WIN" if bucket_won else "LOSS"
        else:
            opp.outcome = "WIN" if not bucket_won else "LOSS"
        opp.closed_at = now
        if opp.virtual_status == "open" and opp.virtual_shares:
            cost = float(opp.virtual_cost or 0.0)
            if opp.outcome == "WIN":
                payout = float(opp.virtual_shares) * 1.00
                opp.virtual_status = "win"
            else:
                payout = 0.0
                opp.virtual_status = "loss"
            opp.virtual_payout = payout
            opp.virtual_pnl = payout - cost
    if iopps:
        await db.commit()
    return list(iopps)


async def _send_intraday_resolution_alert(
    city: City, market: Market, iopps: list, winning_outcome_ids: set, db,
) -> None:
    """⚡ settlement summary for intraday positions — without this the intraday
    learning loop settles silently and the user never sees WIN/LOSS."""
    from app.models.alert import TelegramUser
    from telegram import Bot

    if not settings.telegram_bot_token or not iopps:
        return

    wins = [o for o in iopps if o.outcome == "WIN"]
    losses = [o for o in iopps if o.outcome == "LOSS"]
    if len(wins) > len(losses):
        header = "⚡✅ *Intraday settlement — WIN*"
    elif len(losses) > len(wins):
        header = "⚡❌ *Intraday settlement — LOSS*"
    else:
        header = "⚡🤝 *Intraday settlement*"

    winning_labels_q = await db.execute(
        select(MarketOutcome.bucket_label).where(
            MarketOutcome.id.in_(winning_outcome_ids)
        )
    ) if winning_outcome_ids else None
    winning_labels = (
        [r[0] for r in winning_labels_q.all()] if winning_labels_q is not None else []
    )

    lines = [
        header + "\n#INTRADAY",
        f"📍 {city.name} — {market.event_date.strftime('%b %d, %Y')}",
    ]
    if winning_labels:
        lines.append(f"🏆 Winning bucket: *{', '.join(winning_labels)}*")
    lines.append("")

    total_pnl = 0.0
    pnl_rows = 0
    for opp in iopps:
        oc_res = await db.execute(
            select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id)
        )
        oc = oc_res.scalar_one_or_none()
        label = oc.bucket_label if oc else f"outcome #{opp.outcome_id}"
        emoji = "✅" if opp.outcome == "WIN" else "❌"
        entry_c = (
            round(float(opp.virtual_entry_price) * 100)
            if opp.virtual_entry_price is not None
            else round(float(opp.signals.get("_entry_cost") or 0) * 100)
            if opp.signals else 0
        )
        line = f"{emoji} {label[:35]} {opp.side} @ {entry_c}¢ → {opp.outcome}"
        if opp.virtual_pnl is not None and opp.virtual_shares:
            pnl = float(opp.virtual_pnl)
            total_pnl += pnl
            pnl_rows += 1
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            line += f" | {opp.virtual_shares} sh → {pnl_str} P&L"
        else:
            line += " | tracked only (no virtual buy)"
        lines.append(line)

    if pnl_rows:
        pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
        lines.append("")
        lines.append(f"📊 Intraday P&L: {pnl_str} ({len(wins)}W / {len(losses)}L)")

    text = "\n".join(lines)
    users_result = await db.execute(select(TelegramUser))
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        if user.cities_watched and city.id not in user.cities_watched:
            continue
        try:
            await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send intraday resolution alert to {user.chat_id}: {e}")


async def job_check_resolutions():
    today = date.today()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Market)
            .where(Market.resolved == False, Market.event_date < today)
            .order_by(Market.event_date)
        )
        markets = result.scalars().all()
        if not markets:
            return
        logger.info(f"job_check_resolutions: checking {len(markets)} unresolved past markets")
        any_resolved = False   # האם נסגר משהו בריצה הזו → עדכון מאגר הכישרון
        for market in markets:
            city_result = await db.execute(select(City).where(City.id == market.city_id))
            city = city_result.scalar_one_or_none()
            if not city:
                continue

            # Always try to fetch METAR high (needed for alert display even when
            # resolution source is Polymarket). The day window is the CITY'S
            # LOCAL day — a UTC window includes the previous local evening and
            # can show a contaminated "high" in the alert.
            import pytz as _pytz
            try:
                _tz = _pytz.timezone(city.timezone) if city.timezone else _pytz.utc
            except Exception:
                _tz = _pytz.utc
            day_start = _tz.localize(datetime(
                market.event_date.year, market.event_date.month, market.event_date.day,
            )).astimezone(timezone.utc)
            day_end = day_start + timedelta(days=1)
            temp_result = await db.execute(
                select(sqlfunc.max(MetarObservation.temperature_f)).where(
                    MetarObservation.icao == city.primary_icao,
                    MetarObservation.observed_at >= day_start,
                    MetarObservation.observed_at < day_end,
                )
            )
            actual_high_raw = temp_result.scalar_one_or_none()
            # METAR high is kept only as a reference figure for the alert; it is
            # NOT used to decide WIN/LOSS (Polymarket is authoritative).
            actual_high_f: Optional[float] = float(actual_high_raw) if actual_high_raw is not None else None

            outcomes_result = await db.execute(
                select(MarketOutcome).where(MarketOutcome.market_id == market.id)
            )
            outcomes = outcomes_result.scalars().all()

            # --- Resolution source: Polymarket ONLY ---
            # WIN/LOSS must reflect how Polymarket actually settled the market.
            # METAR (or any temperature computation) can disagree with
            # Polymarket's official result — different station, rounding, or
            # observation window — so it must NEVER decide WIN/LOSS. If
            # Polymarket hasn't settled yet we leave the market unresolved and
            # retry on the next run. The METAR high is still fetched above, but
            # only for display as a reference figure in the alert.
            poly_winning, poly_resolved, poly_note = (
                await _fetch_polymarket_winning_outcomes(market.external_id, outcomes)
            )

            if not poly_resolved:
                logger.info(
                    f"job_check_resolutions: {market.external_id} not yet settled on "
                    f"Polymarket — leaving unresolved (METAR not used for resolution)"
                )
                continue

            winning_outcome_ids: set[int] = set(poly_winning)
            resolution_source = "Polymarket"
            # Store the authoritative winning bucket(s), not the METAR temp,
            # so the recorded resolution can't contradict the WIN/LOSS rows.
            win_labels = [
                o.bucket_label for o in outcomes if o.id in winning_outcome_ids
            ]
            if win_labels:
                res_val = f"{', '.join(win_labels)} (Polymarket)"
            else:
                res_val = f"Polymarket: {poly_note}"
            logger.info(
                f"job_check_resolutions: {market.external_id} → Polymarket: {poly_note}"
            )

            market.resolved = True
            market.resolution_value = res_val
            any_resolved = True
            _mark_outcome_winners(outcomes, winning_outcome_ids)

            # Settle ALL unsettled opportunities on this market — not only the
            # alerted ones. The detector records (and can open virtual buys on)
            # every qualifying bucket while alerting only the best one; with an
            # alert_sent filter the non-alerted positions stayed "open" forever
            # until a manual "Resolve pending" sweep.
            opps_result = await db.execute(
                select(Opportunity)
                .join(MarketOutcome)
                .where(
                    MarketOutcome.market_id == market.id,
                    Opportunity.outcome == None,
                )
            )
            opps = opps_result.scalars().all()
            now = datetime.now(timezone.utc)
            for opp in opps:
                bucket_won = opp.outcome_id in winning_outcome_ids
                if opp.side == "YES":
                    opp.outcome = "WIN" if bucket_won else "LOSS"
                else:
                    opp.outcome = "WIN" if not bucket_won else "LOSS"
                opp.closed_at = now

                if opp.virtual_status == "open" and opp.virtual_shares:
                    cost = float(opp.virtual_cost or 0.0)
                    if opp.outcome == "WIN":
                        payout = float(opp.virtual_shares) * 1.00
                        opp.virtual_payout = payout
                        opp.virtual_status = "win"
                    else:
                        payout = 0.0
                        opp.virtual_payout = payout
                        opp.virtual_status = "loss"
                    opp.virtual_pnl = payout - cost
            await db.commit()

            # Intraday settlement — separate flow, own try/except so it can
            # never break the daily settlement above.
            try:
                intraday_settled = await _settle_intraday_for_market(db, market, winning_outcome_ids)
                if intraday_settled:
                    logger.info(
                        f"job_check_resolutions: settled {len(intraday_settled)} intraday "
                        f"positions for {market.external_id}"
                    )
                    await _send_intraday_resolution_alert(
                        city, market, intraday_settled, winning_outcome_ids, db,
                    )
            except Exception as e:
                logger.error(f"Intraday settlement failed for {market.external_id}: {e}", exc_info=True)

            if opps:
                try:
                    await _send_resolution_alert(
                        city, market, actual_high_f, opps, winning_outcome_ids,
                        db, outcomes, resolution_source,
                    )
                except Exception as e:
                    logger.error(f"Failed to send resolution alert for {market.external_id}: {e}")

        # נסגרו שווקים בריצה הזו → מעדכנים מיד את מאגר דיוק-המודלים, כדי
        # שהמשקולות יראו את התוצאה הטרייה כבר בסריקת החיזוי הבאה.
        # try/except נפרד: כשל בעדכון הכישרון לעולם לא מפיל settlement.
        if any_resolved:
            try:
                from app.analyzers.model_skill import update_model_skill
                await update_model_skill(db)
            except Exception as e:
                logger.error(f"model_skill update after resolutions failed: {e}", exc_info=True)


async def job_retroactive_resolution_fix() -> dict:
    """Re-resolve already-settled markets using Polymarket's authoritative data.

    Updates the global LAST_RETRO_FIX dict with live progress so callers
    can poll for status without waiting for the full run to complete.
    Safe to run multiple times — only changes records that differ from Polymarket.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    LAST_RETRO_FIX.update({
        "status": "running",
        "started_at": now_str,
        "finished_at": None,
        "markets_total": 0,
        "markets_checked": 0,
        "markets_skipped_no_poly": 0,
        "markets_skipped_no_token": 0,
        "opportunities_corrected": 0,
        "corrections": [],
        "error": None,
    })

    try:
        corrected = 0
        skipped_no_poly = 0
        skipped_no_token = 0
        markets_checked = 0
        corrections: list[dict] = []

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Market)
                .where(Market.resolved == True)
                .order_by(Market.event_date.desc())
            )
            markets = result.scalars().all()
            LAST_RETRO_FIX["markets_total"] = len(markets)
            logger.info(f"job_retroactive_resolution_fix: checking {len(markets)} resolved markets")

            for market in markets:
                outcomes_result = await db.execute(
                    select(MarketOutcome).where(MarketOutcome.market_id == market.id)
                )
                outcomes = outcomes_result.scalars().all()
                if not outcomes:
                    continue

                has_token = any(o.token_id for o in outcomes)
                if not has_token:
                    skipped_no_token += 1
                    LAST_RETRO_FIX["markets_skipped_no_token"] = skipped_no_token
                    continue

                markets_checked += 1
                LAST_RETRO_FIX["markets_checked"] = markets_checked
                poly_winning, poly_resolved, poly_note = (
                    await _fetch_polymarket_winning_outcomes(market.external_id, outcomes)
                )

                if not poly_resolved:
                    skipped_no_poly += 1
                    LAST_RETRO_FIX["markets_skipped_no_poly"] = skipped_no_poly
                    logger.debug(
                        f"retroactive_fix: {market.external_id} not yet resolved on Polymarket"
                    )
                    continue

                # Record the authoritative winning bucket(s) for every resolved
                # market, even when no opportunity needs correcting — this keeps
                # MarketOutcome.won populated for model-accuracy scoring.
                _mark_outcome_winners(outcomes, set(poly_winning))

                # Fetch all settled opps on this market.
                opps_result = await db.execute(
                    select(Opportunity)
                    .join(MarketOutcome)
                    .where(
                        MarketOutcome.market_id == market.id,
                        Opportunity.outcome.in_(["WIN", "LOSS"]),
                    )
                )
                opps = opps_result.scalars().all()

                market_corrected = 0
                for opp in opps:
                    bucket_won = opp.outcome_id in poly_winning
                    if opp.side == "YES":
                        correct_outcome = "WIN" if bucket_won else "LOSS"
                    else:
                        correct_outcome = "WIN" if not bucket_won else "LOSS"

                    if opp.outcome == correct_outcome:
                        continue

                    old_outcome = opp.outcome
                    opp.outcome = correct_outcome

                    if opp.virtual_shares:
                        cost = float(opp.virtual_cost or 0.0)
                        if correct_outcome == "WIN":
                            payout = float(opp.virtual_shares) * 1.00
                            opp.virtual_payout = payout
                            opp.virtual_status = "win"
                        else:
                            payout = 0.0
                            opp.virtual_payout = payout
                            opp.virtual_status = "loss"
                        opp.virtual_pnl = payout - cost

                    corrected += 1
                    market_corrected += 1

                    oc_r = await db.execute(
                        select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id)
                    )
                    oc = oc_r.scalar_one_or_none()
                    bucket_label = oc.bucket_label if oc else f"#{opp.outcome_id}"
                    entry = {
                        "market": market.external_id,
                        "opp_id": opp.id,
                        "bucket": bucket_label,
                        "side": opp.side,
                        "was": old_outcome,
                        "now": correct_outcome,
                    }
                    corrections.append(entry)
                    LAST_RETRO_FIX["opportunities_corrected"] = corrected
                    LAST_RETRO_FIX["corrections"] = corrections
                    logger.info(
                        f"retroactive_fix: corrected opp #{opp.id} market={market.external_id} "
                        f"bucket={bucket_label!r} side={opp.side} {old_outcome} → {correct_outcome}"
                    )

                if market_corrected > 0:
                    if market.resolution_value and "(Polymarket)" not in market.resolution_value:
                        market.resolution_value = market.resolution_value.rstrip() + " (Polymarket)"
                    elif not market.resolution_value:
                        market.resolution_value = f"Polymarket: {poly_note}"
                    await db.commit()

            # Flush any remaining pending changes (e.g. `won` flags set on
            # resolved markets that needed no opportunity corrections).
            await db.commit()

        result_summary = {
            "status": "done",
            "started_at": now_str,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "markets_total": LAST_RETRO_FIX["markets_total"],
            "markets_checked": markets_checked,
            "markets_skipped_no_poly": skipped_no_poly,
            "markets_skipped_no_token": skipped_no_token,
            "opportunities_corrected": corrected,
            "corrections": corrections,
            "error": None,
        }
        LAST_RETRO_FIX.update(result_summary)
        logger.info(
            f"job_retroactive_resolution_fix complete: checked={markets_checked} "
            f"corrected={corrected} no_poly={skipped_no_poly} no_token={skipped_no_token}"
        )
        return result_summary

    except Exception as e:
        LAST_RETRO_FIX["status"] = "error"
        LAST_RETRO_FIX["error"] = str(e)
        LAST_RETRO_FIX["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.error(f"job_retroactive_resolution_fix failed: {e}", exc_info=True)
        raise
