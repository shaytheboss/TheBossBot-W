import asyncio
import logging
import re
from datetime import date, datetime, timezone
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


async def _discover_city_markets(city: City, today: date, db: AsyncSession) -> int:
    """Fetch today's Polymarket event for a city and seed markets + outcomes."""
    slug = (
        f"highest-temperature-in-{city.polymarket_slug}"
        f"-on-{today.strftime('%B').lower()}-{today.day}-{today.year}"
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
        logger.info(f"No Polymarket event found: {slug}")
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
        question=event.get("title") or f"Highest temperature in {city.name} on {today}",
        event_date=today,
        resolution_time=resolution_time,
        resolution_source=event.get("description", "Wunderground"),
    )
    db.add(market)
    await db.flush()

    count = 0
    for m in event.get("markets", []):
        question = m.get("question", "")

        # Extract temperature thresholds from question text
        temps = [int(t) for t in re.findall(r"(\d+)\s*°?F", question)]
        bucket_min = temps[0] if len(temps) >= 1 else None
        bucket_max = temps[1] if len(temps) >= 2 else None
        bucket_label = question[:50] if question else (f"{bucket_min}-{bucket_max}°F" if bucket_max else f"{bucket_min}+°F")

        # Get YES token ID from tokens array
        token_id: Optional[str] = None
        for token in m.get("tokens", []):
            if str(token.get("outcome", "")).lower() == "yes":
                token_id = token.get("tokenId")
                break
        if not token_id:
            ids = m.get("clobTokenIds") or []
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
    """Discover today's Polymarket weather markets for all active cities."""
    today = date.today()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True, City.polymarket_slug != None))
        cities = result.scalars().all()
        for city in cities:
            try:
                found = await _discover_city_markets(city, today, db)
                if found:
                    logger.info(f"{city.name}: {found} outcomes discovered")
            except Exception as e:
                logger.error(f"Market discovery failed for {city.name}: {e}", exc_info=True)


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
        for city in cities:
            if city.nws_lat is None or city.nws_lon is None:
                continue
            try:
                await nws_col.collect_and_store(city.id, float(city.nws_lat), float(city.nws_lon), today, db)
            except Exception as e:
                logger.error(f"NWS job failed for {city.name}: {e}")


async def job_fetch_models():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        today = date.today()
        for city in cities:
            if city.nws_lat is None or city.nws_lon is None:
                continue
            lat, lon = float(city.nws_lat), float(city.nws_lon)
            for model in ("gfs", "ecmwf"):
                try:
                    await gfs_col.collect_and_store(city.id, lat, lon, today, db, model)
                except Exception as e:
                    logger.error(f"{model} job failed for {city.name}: {e}")


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
            for opp in opportunities:
                try:
                    await send_opportunity_alert(opp, db)
                except Exception as e:
                    logger.error(f"Failed to send alert for opportunity {opp.id}: {e}")
        except Exception as e:
            logger.error(f"Analyzer job failed: {e}", exc_info=True)
