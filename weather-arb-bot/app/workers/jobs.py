import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.market import Market, MarketOutcome
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

# How many days ahead to scan Polymarket for markets
MARKET_DISCOVERY_DAYS_AHEAD = 7
# How many days ahead to pull weather forecasts
FORECAST_DAYS_AHEAD = 7


async def _discover_city_markets(city: City, target_date: date, db: AsyncSession) -> int:
    """Fetch Polymarket event for a city/date and seed markets + outcomes."""
    slug = (
        f"highest-temperature-in-{city.polymarket_slug}"
        f"-on-{target_date.strftime('%B').lower()}-{target_date.day}-{target_date.year}"
    )

    existing = await db.execute(select(Market).where(Market.external_id == slug))
    if existing.scalar_one_or_none():
        return 0

    try:
        resp = await poly_col._get(f"{GAMMA_API}/events", params={"slug": slug})
        events = resp.json()
    except Exception as e:
        logger.warning(f"Gamma API failed for {slug}: {e}")
        return 0

    if not events:
        logger.debug(f"No Polymarket event found: {slug}")
        return 0

    event = events[0] if isinstance(events, list) else events

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
        question=event.get("title") or f"Highest temperature in {city.name} on {target_date}",
        event_date=target_date,
        resolution_time=resolution_time,
        resolution_source=event.get("description", "Wunderground"),
    )
    db.add(market)
    await db.flush()

    count = 0
    for m in event.get("markets", []):
        question = m.get("question", "")

        temps = [int(t) for t in re.findall(r"(\d+)\s*°?F", question)]
        bucket_min = temps[0] if len(temps) >= 1 else None
        bucket_max = temps[1] if len(temps) >= 2 else None
        bucket_label = (
            question[:50] if question
            else (f"{bucket_min}-{bucket_max}°F" if bucket_max else f"{bucket_min}+°F")
        )

        token_id: Optional[str] = None
        for token in m.get("tokens", []):
            if str(token.get("outcome", "")).lower() == "yes":
                token_id = token.get("tokenId")
                break
        if not token_id:
            ids = m.get("clobTokenIds") or []
            if isinstance(ids, str):
                # Sometimes clobTokenIds is a JSON-encoded string
                import json
                try:
                    ids = json.loads(ids)
                except json.JSONDecodeError:
                    ids = []
            token_id = ids[0] if ids else None

        outcome = MarketOutcome(
            market_id=market.id,
            bucket_label=bucket_label,
            bucket_min=bucket_min,
            bucket_max=bucket_max,
            token_id=token_id,
        )
        db.add(outcome)
        count += 1

    await db.commit()
    logger.info(f"Discovered {count} outcomes for {slug}")
    return count


async def job_discover_markets():
    """Discover Polymarket weather markets for all active cities, today + N future days."""
    today = date.today()
    dates = [today + timedelta(days=i) for i in range(MARKET_DISCOVERY_DAYS_AHEAD)]
    total = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(City).where(City.active == True, City.polymarket_slug != None)
        )
        cities = result.scalars().all()
        if not cities:
            logger.warning("job_discover_markets: no cities with polymarket_slug found")
            return
        logger.info(f"job_discover_markets: scanning {len(cities)} cities x {len(dates)} dates")
        for city in cities:
            for target_date in dates:
                try:
                    found = await _discover_city_markets(city, target_date, db)
                    total += found
                except Exception as e:
                    logger.error(
                        f"Market discovery failed for {city.name} {target_date}: {e}",
                        exc_info=True,
                    )
    logger.info(f"job_discover_markets: total {total} new outcomes added")


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
    """Fetch NWS forecast for today + N future days."""
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
    """Fetch GFS/ECMWF model data for today + N future days."""
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
    """Fetch current prices for all tracked outcomes that have a token_id."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MarketOutcome)
            .join(Market)
            .where(Market.resolved == False, MarketOutcome.token_id != None)
        )
        outcomes = result.scalars().all()
        if not outcomes:
            return
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
