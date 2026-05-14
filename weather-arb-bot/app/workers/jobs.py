import asyncio
import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

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

logger = logging.getLogger(__name__)

metar_col = MetarCollector()
wunder_col = WundergroundCollector()
nws_col = NWSCollector()
gfs_col = GFSCollector()
pirep_col = PirepCollector()
poly_col = PolymarketCollector()

GAMMA_API = "https://gamma-api.polymarket.com"
FORECAST_DAYS_AHEAD = 7

# Search terms for Polymarket Gamma /events?q=<term> — matches the working
# logic from polymarketweatherassistwebpage.
POLYMARKET_QUERIES = [
    "temperature",
    "highest temperature",
    "degrees fahrenheit",
    "hottest",
    "warmest",
    "high temp",
]

# City slug aliases — Polymarket sometimes uses different names than ours.
CITY_ALIAS_OVERRIDES = {
    "nyc": ["nyc", "new-york", "new-york-city", "newyork"],
    "los-angeles": ["los-angeles", "la", "losangeles"],
    "san-francisco": ["san-francisco", "sf", "sanfrancisco"],
    "washington-dc": ["washington-dc", "dc", "washington"],
    "philadelphia": ["philadelphia", "philly"],
    "dallas": ["dallas", "dfw", "dallas-fort-worth"],
}

# ICAO codes Polymarket weather markets resolve via, mapped to our city slugs.
# Source: polymarketweatherassistwebpage working code.
POLYMARKET_ICAO_TO_CITY_SLUG = {
    "KNYC": "nyc",          # NYC — Central Park
    "KLGA": "nyc",          # alt NYC
    "KJFK": "nyc",          # alt NYC
    "KLAX": "los-angeles",
    "KMIA": "miami",
    "KDEN": "denver",
    "KORD": "chicago",
    "KAUS": "austin",
    "KIAH": "houston",
    "KHOU": "houston",
    "KDFW": "dallas",
    "KPHL": "philadelphia",
    "KATL": "atlanta",
    "KSEA": "seattle",
    "KSFO": "san-francisco",
    "KDCA": "washington-dc",
    "KPHX": "phoenix",
    "KBOS": "boston",
    "KLAS": "las-vegas",
    "KPDX": "portland",
    "KMSP": "minneapolis",
    "KDTW": "detroit",
    "KSAN": "san-diego",
    "KTPA": "tampa",
    "KMCO": "orlando",
    "EGLL": "london",
}

_MONTH_BY_NAME = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH_BY_NAME.update({
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
})
_MONTHS_RX = "|".join(sorted(_MONTH_BY_NAME, key=len, reverse=True))

# Wunderground URL in description: last path segment is the station ICAO
WUNDERGROUND_ICAO_RX = re.compile(
    r"wunderground\.com/[\w/\-]+/([A-Z]{4})(?=[/\s\)\.,]|$)", re.IGNORECASE
)

# Date in question prose: "on May 13", "for May 13, 2026"
_DATE_PROSE_RX = re.compile(
    rf"(?:on|for)\s+({_MONTHS_RX})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:[,\s]+(\d{{4}}))?",
    re.IGNORECASE,
)
# Date in slug: "may-13-2026" or "may-13"
_DATE_SLUG_RX = re.compile(
    rf"({_MONTHS_RX})-(\d{{1,2}})(?:-(\d{{4}}))?", re.IGNORECASE,
)


def _city_aliases(city: City) -> list[str]:
    slug = city.polymarket_slug
    if not slug:
        return []
    return CITY_ALIAS_OVERRIDES.get(slug, [slug])


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


async def _search_event_slugs(query: str, limit: int = 100) -> set[str]:
    """Polymarket Gamma /events?q=<term> returns matching events (slugs only;
    the markets[] array comes back empty)."""
    try:
        resp = await poly_col._get(
            f"{GAMMA_API}/events",
            params={"q": query, "active": "true", "closed": "false", "limit": limit},
        )
        events = resp.json()
    except Exception as e:
        logger.warning(f"Gamma /events?q={query!r} failed: {e}")
        return set()
    slugs: set[str] = set()
    if isinstance(events, list):
        for e in events:
            s = e.get("slug")
            if s:
                slugs.add(s)
    return slugs


async def _fetch_event_by_slug(slug: str) -> Optional[dict]:
    """Fetch single event by slug — returns full event with populated markets[]."""
    try:
        resp = await poly_col._get(
            f"{GAMMA_API}/events", params={"slug": slug, "limit": 1}
        )
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data.get("slug"):
            return data
    except Exception as e:
        logger.warning(f"Gamma /events?slug={slug} failed: {e}")
    return None


def _match_event_to_city(event: dict, alias_to_city: dict[str, City]) -> Optional[City]:
    """Try several ways to match an event to one of our cities."""
    slug = (event.get("slug") or "").lower()
    title = (event.get("title") or "").lower()
    description = event.get("description") or ""

    # 1. ICAO from Wunderground link in description (most reliable)
    icao = _extract_icao_from_description(description)
    if not icao:
        for m in event.get("markets", []) or []:
            icao = _extract_icao_from_description(m.get("description") or "")
            if icao:
                break
    if icao:
        city_slug = POLYMARKET_ICAO_TO_CITY_SLUG.get(icao)
        if city_slug and city_slug in alias_to_city:
            return alias_to_city[city_slug]

    # 2. Match by alias appearing in slug/title
    for alias, city in alias_to_city.items():
        # avoid spurious substring hits like "san" matching "san-jose"
        token = f"-{alias}-"
        padded_slug = f"-{slug}-"
        if token in padded_slug or alias in title.split():
            return city
    return None


async def _ingest_event(event: dict, city: City, db: AsyncSession) -> int:
    """Insert market + outcomes for an event matched to a city."""
    slug = event.get("slug")
    if not slug:
        return 0

    existing = await db.execute(select(Market).where(Market.external_id == slug))
    if existing.scalar_one_or_none():
        return 0

    # Resolve date — prefer slug, fall back to title
    target_date = _parse_date(slug) or _parse_date(event.get("title") or "")
    if not target_date:
        logger.debug(f"No date parsed from event slug={slug}")
        return 0
    today = date.today()
    if target_date < today or target_date > today + timedelta(days=FORECAST_DAYS_AHEAD):
        return 0

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
        question=event.get("title") or f"Highest temp in {city.name} on {target_date}",
        event_date=target_date,
        resolution_time=resolution_time,
        resolution_source=(event.get("description") or "")[:500] or None,
    )
    db.add(market)
    await db.flush()

    count = 0
    for m in event.get("markets", []) or []:
        question = m.get("question", "") or m.get("groupItemTitle", "")
        temps = [int(t) for t in re.findall(r"(\d+)\s*°?F", question)]
        bucket_min = temps[0] if len(temps) >= 1 else None
        bucket_max = temps[1] if len(temps) >= 2 else None
        bucket_label = (
            question[:50] if question
            else (f"{bucket_min}-{bucket_max}°F" if bucket_max else f"{bucket_min}+°F")
        )

        token_id: Optional[str] = None
        for token in m.get("tokens", []) or []:
            if str(token.get("outcome", "")).lower() == "yes":
                token_id = token.get("tokenId")
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
            bucket_label=bucket_label or "unknown",
            bucket_min=bucket_min,
            bucket_max=bucket_max,
            token_id=token_id,
        )
        db.add(outcome)
        count += 1

    await db.commit()
    logger.info(
        f"Discovered market: city={city.name} date={target_date} "
        f"slug={slug} outcomes={count}"
    )
    return count


async def job_discover_markets():
    """Discover Polymarket weather markets via q-based search.

    Strategy from polymarketweatherassistwebpage (proven working):
      1. Search Gamma /events?q=<term> with multiple weather terms
      2. Collect unique slugs (search returns events with EMPTY markets[])
      3. Fetch each event individually by slug (returns full event)
      4. Extract resolution ICAO from description → match to our cities
      5. Fall back to slug/title alias matching
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(City).where(City.active == True, City.polymarket_slug != None)
        )
        cities = list(result.scalars().all())
        if not cities:
            logger.warning("job_discover_markets: no cities with polymarket_slug")
            return

        alias_to_city: dict[str, City] = {}
        for city in cities:
            for alias in _city_aliases(city):
                alias_to_city[alias.lower()] = city

        # 1. Collect slugs across all search terms
        all_slugs: set[str] = set()
        for q in POLYMARKET_QUERIES:
            found = await _search_event_slugs(q, limit=100)
            all_slugs |= found
        logger.info(
            f"job_discover_markets: found {len(all_slugs)} unique slugs across "
            f"{len(POLYMARKET_QUERIES)} search queries"
        )
        if not all_slugs:
            return

        # 2. Fetch full event for each slug (cap at 200 for safety)
        new_outcomes = 0
        matched_cities: dict[str, int] = {}
        unmatched: list[str] = []
        for slug in list(all_slugs)[:200]:
            event = await _fetch_event_by_slug(slug)
            if not event:
                continue
            city = _match_event_to_city(event, alias_to_city)
            if not city:
                unmatched.append(slug)
                continue
            try:
                added = await _ingest_event(event, city, db)
                if added > 0:
                    new_outcomes += added
                    matched_cities[city.name] = matched_cities.get(city.name, 0) + 1
            except Exception as e:
                logger.error(f"_ingest_event failed for slug={slug}: {e}", exc_info=True)

        logger.info(
            f"job_discover_markets: ingested={new_outcomes} outcomes "
            f"matched_cities={matched_cities}"
        )
        if unmatched:
            sample = unmatched[:10]
            logger.info(f"job_discover_markets: unmatched slugs (sample): {sample}")


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
    from app.config import settings
    from telegram import Bot

    if not settings.telegram_bot_token:
        return

    wins = [o for o in opps if o.outcome == "WIN"]
    losses = [o for o in opps if o.outcome == "LOSS"]
    total = len(wins) + len(losses)
    if total == 0:
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
            logger.info(f"Resolving {market.external_id}: actual high = {actual_high_f}°F")

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
