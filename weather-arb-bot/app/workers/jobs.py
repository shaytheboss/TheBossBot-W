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

# Days ahead to track
FORECAST_DAYS_AHEAD = 7

# Polymarket sometimes uses different slug names than our canonical city slug.
CITY_ALIAS_OVERRIDES = {
    "nyc": ["nyc", "new-york", "new-york-city", "newyork"],
    "la": ["la", "los-angeles", "losangeles"],
    "san-francisco": ["san-francisco", "sf", "sanfrancisco"],
    "washington-dc": ["washington-dc", "dc", "washington"],
    "philadelphia": ["philadelphia", "philly"],
    "dallas": ["dallas", "dfw", "dallas-fort-worth"],
}

# Match slugs like:
#   highest-temperature-in-nyc-on-may-13-2026
#   highest-temperature-in-nyc-on-may-13
#   nyc-highest-temperature-on-may-13-2026
TEMP_SLUG_RX_A = re.compile(
    r"^highest[-_]temperature[-_]in[-_]([a-z][a-z0-9-]*?)[-_]on[-_]"
    r"([a-z]+)[-_](\d{1,2})(?:[-_](\d{4}))?$"
)
TEMP_SLUG_RX_B = re.compile(
    r"^([a-z][a-z0-9-]*?)[-_]highest[-_]temperature[-_]on[-_]"
    r"([a-z]+)[-_](\d{1,2})(?:[-_](\d{4}))?$"
)

_MONTH_BY_NAME = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def _city_aliases(city: City) -> list[str]:
    slug = city.polymarket_slug
    if not slug:
        return []
    return CITY_ALIAS_OVERRIDES.get(slug, [slug])


def _parse_temp_slug(slug: str):
    """Return (city_alias, date) if slug looks like a daily-high temp market, else None."""
    s = slug.lower()
    for rx in (TEMP_SLUG_RX_A, TEMP_SLUG_RX_B):
        m = rx.match(s)
        if not m:
            continue
        city_alias, month_name, day_str, year_str = m.groups()
        month = _MONTH_BY_NAME.get(month_name)
        if not month:
            continue
        try:
            year = int(year_str) if year_str else date.today().year
            day = int(day_str)
            return city_alias, date(year, month, day)
        except ValueError:
            continue
    return None


async def _fetch_all_active_events() -> list[dict]:
    """Paginate through Polymarket Gamma API for all open events."""
    events: list[dict] = []
    offset = 0
    page_size = 100
    safety_limit = 50  # 50 pages * 100 = up to 5000 events
    for _ in range(safety_limit):
        try:
            resp = await poly_col._get(
                f"{GAMMA_API}/events",
                params={"closed": "false", "limit": page_size, "offset": offset},
            )
            batch = resp.json()
        except Exception as e:
            logger.error(f"Gamma events fetch failed at offset={offset}: {e}")
            break
        if not batch:
            break
        events.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return events


async def _create_market_from_event(
    event: dict, city: City, target_date: date, db: AsyncSession
) -> int:
    """Insert Market + MarketOutcome rows from a Polymarket event payload."""
    slug = event.get("slug") or f"{city.polymarket_slug}-{target_date.isoformat()}"

    existing = await db.execute(select(Market).where(Market.external_id == slug))
    if existing.scalar_one_or_none():
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
        resolution_source=event.get("description", "")[:500] if event.get("description") else None,
    )
    db.add(market)
    await db.flush()

    count = 0
    for m in event.get("markets", []):
        question = m.get("question", "") or m.get("groupItemTitle", "")
        temps = [int(t) for t in re.findall(r"(\d+)\s*°?F", question)]
        bucket_min = temps[0] if len(temps) >= 1 else None
        bucket_max = temps[1] if len(temps) >= 2 else None
        bucket_label = (
            question[:50]
            if question
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
    logger.info(f"Discovered {count} outcomes for {slug} ({city.name} {target_date})")
    return count


async def job_discover_markets():
    """Pull all open Polymarket events, find daily-high temperature markets, match to our cities."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(City).where(City.active == True, City.polymarket_slug != None)
        )
        cities = result.scalars().all()
        if not cities:
            logger.warning("job_discover_markets: no cities with polymarket_slug")
            return

        # Build alias -> city map
        alias_to_city: dict[str, City] = {}
        for city in cities:
            for alias in _city_aliases(city):
                alias_to_city[alias.lower()] = city

        events = await _fetch_all_active_events()
        logger.info(f"job_discover_markets: fetched {len(events)} open events from Gamma")

        # Optional: log temperature-looking events for debugging
        temp_events = []
        unmatched_aliases: set[str] = set()
        today = date.today()
        horizon = today + timedelta(days=FORECAST_DAYS_AHEAD)
        total_new = 0

        for event in events:
            slug = (event.get("slug") or "").lower()
            if "temperature" not in slug:
                continue
            temp_events.append(slug)
            parsed = _parse_temp_slug(slug)
            if not parsed:
                continue
            city_alias, target_date = parsed
            if target_date < today or target_date > horizon:
                continue
            city = alias_to_city.get(city_alias)
            if not city:
                unmatched_aliases.add(city_alias)
                continue
            try:
                total_new += await _create_market_from_event(event, city, target_date, db)
            except Exception as e:
                logger.error(f"_create_market_from_event failed for {slug}: {e}", exc_info=True)

        logger.info(
            f"job_discover_markets: temp_events_seen={len(temp_events)} "
            f"unmatched_city_aliases={sorted(unmatched_aliases)} new_outcomes={total_new}"
        )
        if temp_events and total_new == 0:
            # Help debugging: show first few slugs that we saw but didn't import
            sample = temp_events[:8]
            logger.info(f"job_discover_markets: sample temperature slugs seen: {sample}")


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
                logger.debug(f"No METAR for {city.name} {market.event_date} — skip resolution")
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
