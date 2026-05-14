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
from app.collectors.pirep_collector import PirepCollector
from app.collectors.polymarket_collector import PolymarketCollector
from app.analyzers.opportunity_detector import detect_opportunities
from app.bot.telegram_bot import send_opportunity_alert
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
pirep_col = PirepCollector()
poly_col = PolymarketCollector()

FORECAST_DAYS_AHEAD = 7
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
    r"wunderground\.com/[\w/\-]+/([A-Z]{4})(?=[/\s\)\.,]|$)", re.IGNORECASE
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

# Skip markets whose question text contains any of these tokens (case-insensitive).
# For now we only trade "highest temperature" markets — daily-low markets are disabled.
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
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _extract_icao_from_description(desc: str) -> Optional[str]:
    if not desc:
        return None
    m = WUNDERGROUND_ICAO_RX.search(desc)
    return m.group(1).upper() if m else None


def _parse_temp_range(text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse temperature bucket bounds from a label string.

    Handles (in priority order):
      "65-70°F" / "65 to 70"     → (65, 70)
      "85+°F" / "X or above/higher/over/more" → (X, None)
      "X or below/lower/under/less"            → (None, X)
      "below/under/less than X"                → (None, X)
      "above/over/greater than X"              → (X, None)
      "11°C" (single exact value)              → (11, 12)  ← treated as [X, X+1)
    """
    if not text:
        return None, None
    t = text.lower().replace("°", "").strip()

    m = re.search(r"(\d+)\s*(?:-|to)\s*(\d+)\s*[fc]?", t)
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
        return v, v + 1

    return None, None


def _is_celsius_bucket(label: str) -> bool:
    """Return True if the bucket label uses Celsius units."""
    lo = label.lower()
    if "°c" in lo:
        return True
    if re.search(r"\d\s*c(?:\s|$|or\b|/)", lo):
        return True
    return False


def _c_to_f(celsius: Optional[int]) -> Optional[int]:
    if celsius is None:
        return None
    return round(celsius * 9 / 5 + 32)


def _parse_bucket(label: str) -> tuple[Optional[int], Optional[int]]:
    """Parse a bucket label into (min, max) in °F (converting from Celsius if needed)."""
    bmin, bmax = _parse_temp_range(label)
    if _is_celsius_bucket(label):
        bmin = _c_to_f(bmin)
        bmax = _c_to_f(bmax)
    return bmin, bmax


async def _refresh_outcome_bounds(db: AsyncSession, market: Market, raw_markets: list) -> int:
    """Re-parse every outcome of an existing market and update bucket_min/max if changed.

    Needed because rows ingested before the parser/Celsius fixes have wrong bounds.
    Returns the count of outcomes updated.
    """
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

        bmin, bmax = _parse_bucket(gtitle)
        if bmin is None and bmax is None:
            bmin, bmax = _parse_bucket(question)

        if target.bucket_min != bmin or target.bucket_max != bmax:
            logger.info(
                f"REFRESH outcome id={target.id} label={bucket_label!r} "
                f"({target.bucket_min},{target.bucket_max}) → ({bmin},{bmax})"
            )
            target.bucket_min = bmin
            target.bucket_max = bmax
            updated += 1

    if updated:
        await db.commit()
    return updated


async def _ingest_event(event: dict, city: City, db: AsyncSession) -> tuple[int, int]:
    """Ingest a Polymarket event. Returns (new_outcomes, refreshed_outcomes).

    If the market already exists, re-parse and refresh existing outcome bounds
    (so old rows with wrong bucket_min/max get corrected on next discovery).
    Skips markets whose title says "lowest" — we only trade highest-temp for now.
    """
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

        bucket_min, bucket_max = _parse_bucket(gtitle)
        if bucket_min is None and bucket_max is None:
            bucket_min, bucket_max = _parse_bucket(question)

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
                    await gfs_col.collect_ensemble_and_store(city.id, lat, lon, d, db)
                except Exception as e:
                    logger.error(f"GFS ensemble job failed for {city.name} {d}: {e}")


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


async def job_run_analyzer():
    async with AsyncSessionLocal() as db:
        try:
            opportunities = await detect_opportunities(db)
            if opportunities:
                logger.info(f"job_run_analyzer: {len(opportunities)} opportunities found")
            for opp in opportunities:
                try:
                    await send_opportunity_alert(opp, db)
                except Exception as e:
                    logger.error(f"Failed to send alert for opportunity {opp.id}: {e}")
        except Exception as e:
            logger.error(f"Analyzer job failed: {e}", exc_info=True)


async def _send_resolution_alert(
    city: City, market: Market, actual_high_f: float, opps: list, winning_outcome_ids: set, db
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
    lines = [
        header,
        f"\U0001f4cd {city.name} (`{city.primary_icao}`) — {market.event_date.strftime('%b %d, %Y')}",
        f"\U0001f321️ Actual high: *{actual_high_f}°F*",
        f"[Polymarket]({poly_url})",
        "",
    ]
    for opp in opps:
        oc_res = await db.execute(select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id))
        oc = oc_res.scalar_one_or_none()
        label = oc.bucket_label if oc else f"outcome #{opp.outcome_id}"
        emoji = "✅" if opp.outcome == "WIN" else "❌"
        lines.append(
            f"{emoji} {label[:35]} {opp.side} @ {round(float(opp.market_price)*100)}¢ → {opp.outcome}"
        )

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
        for market in markets:
            city_result = await db.execute(select(City).where(City.id == market.city_id))
            city = city_result.scalar_one_or_none()
            if not city:
                continue

            day_start = datetime(
                market.event_date.year, market.event_date.month, market.event_date.day,
                tzinfo=timezone.utc,
            )
            day_end = day_start + timedelta(days=1)
            temp_result = await db.execute(
                select(sqlfunc.max(MetarObservation.temperature_f)).where(
                    MetarObservation.icao == city.primary_icao,
                    MetarObservation.observed_at >= day_start,
                    MetarObservation.observed_at < day_end,
                )
            )
            actual_high_raw = temp_result.scalar_one_or_none()
            if actual_high_raw is None:
                continue
            actual_high_f = float(actual_high_raw)

            outcomes_result = await db.execute(
                select(MarketOutcome).where(MarketOutcome.market_id == market.id)
            )
            outcomes = outcomes_result.scalars().all()
            winning_outcome_ids: set = set()
            for outcome in outcomes:
                bn, bx = outcome.bucket_min, outcome.bucket_max
                if bn is not None and bx is not None:
                    if bn <= actual_high_f < bx:
                        winning_outcome_ids.add(outcome.id)
                elif bn is not None and bx is None:
                    if actual_high_f >= bn:
                        winning_outcome_ids.add(outcome.id)
                elif bn is None and bx is not None:
                    if actual_high_f <= bx:
                        winning_outcome_ids.add(outcome.id)

            market.resolved = True
            market.resolution_value = f"{actual_high_f}°F"

            opps_result = await db.execute(
                select(Opportunity)
                .join(MarketOutcome)
                .where(
                    MarketOutcome.market_id == market.id,
                    Opportunity.alert_sent == True,
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
            await db.commit()

            if opps:
                try:
                    await _send_resolution_alert(city, market, actual_high_f, opps, winning_outcome_ids, db)
                except Exception as e:
                    logger.error(f"Failed to send resolution alert for {market.external_id}: {e}")
