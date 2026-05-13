"""
Entry point for the background scheduler process.
Run as: python -m app.workers.scheduler
"""
import asyncio
import logging
import signal
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.workers.jobs import (
    job_fetch_metars,
    job_fetch_wunderground,
    job_fetch_nws,
    job_fetch_models,
    job_fetch_pireps,
    job_fetch_polymarket,
    job_run_analyzer,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    now = datetime.now()

    scheduler.add_job(
        job_fetch_metars,
        IntervalTrigger(seconds=settings.metar_fetch_interval),
        id="metar",
        name="Fetch METARs",
        next_run_time=now,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        job_fetch_wunderground,
        IntervalTrigger(seconds=settings.wunderground_fetch_interval),
        id="wunderground",
        name="Fetch Wunderground forecasts",
        next_run_time=now,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        job_fetch_nws,
        IntervalTrigger(seconds=3600),
        id="nws",
        name="Fetch NWS forecasts",
        next_run_time=now,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        job_fetch_models,
        IntervalTrigger(seconds=3600),
        id="models",
        name="Fetch GFS/ECMWF model data",
        next_run_time=now,
        max_instances=1,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_fetch_pireps,
        IntervalTrigger(seconds=900),
        id="pireps",
        name="Fetch PIREPs",
        next_run_time=now,
        max_instances=1,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        job_fetch_polymarket,
        IntervalTrigger(seconds=settings.polymarket_fetch_interval),
        id="polymarket",
        name="Fetch Polymarket prices",
        next_run_time=now,
        max_instances=1,
        misfire_grace_time=15,
    )
    scheduler.add_job(
        job_run_analyzer,
        IntervalTrigger(seconds=settings.analyzer_run_interval),
        id="analyzer",
        name="Run opportunity analyzer",
        next_run_time=now,
        max_instances=1,
        misfire_grace_time=60,
    )

    return scheduler


async def main():
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("Scheduler started. Jobs: %s", [j.id for j in scheduler.get_jobs()])

    stop_event = asyncio.Event()

    def _stop(sig, frame):
        logger.info(f"Received signal {sig}, shutting down scheduler...")
        scheduler.shutdown(wait=False)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    await stop_event.wait()


if __name__ == "__main__":
    asyncio.run(main())
