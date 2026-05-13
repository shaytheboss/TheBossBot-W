import logging
from contextlib import asynccontextmanager
from datetime import datetime

import sentry_sdk
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from app.config import settings
from app.api import cities, markets, opportunities, users, health

logger = logging.getLogger(__name__)

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env)

_bot_app = None
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_app, _scheduler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Weather Arbitrage Bot starting up...")

    # Telegram bot
    if settings.telegram_bot_token:
        from app.bot.telegram_bot import get_app
        _bot_app = get_app()
        await _bot_app.initialize()
        await _bot_app.start()
        logger.info("Telegram bot initialized (webhook mode)")

    # Background scheduler (same process so crashes are visible in logs)
    try:
        from app.workers.jobs import (
            job_fetch_metars, job_fetch_wunderground, job_fetch_nws,
            job_fetch_models, job_fetch_pireps, job_fetch_polymarket,
            job_run_analyzer,
        )
        now = datetime.now()
        _scheduler = AsyncIOScheduler()
        _scheduler.add_job(job_fetch_metars, IntervalTrigger(seconds=settings.metar_fetch_interval),
                           id="metar", next_run_time=now, max_instances=1, misfire_grace_time=60)
        _scheduler.add_job(job_fetch_wunderground, IntervalTrigger(seconds=settings.wunderground_fetch_interval),
                           id="wunderground", next_run_time=now, max_instances=1, misfire_grace_time=300)
        _scheduler.add_job(job_fetch_nws, IntervalTrigger(seconds=3600),
                           id="nws", next_run_time=now, max_instances=1, misfire_grace_time=300)
        _scheduler.add_job(job_fetch_models, IntervalTrigger(seconds=3600),
                           id="models", next_run_time=now, max_instances=1, misfire_grace_time=600)
        _scheduler.add_job(job_fetch_pireps, IntervalTrigger(seconds=900),
                           id="pireps", next_run_time=now, max_instances=1, misfire_grace_time=120)
        _scheduler.add_job(job_fetch_polymarket, IntervalTrigger(seconds=settings.polymarket_fetch_interval),
                           id="polymarket", next_run_time=now, max_instances=1, misfire_grace_time=15)
        _scheduler.add_job(job_run_analyzer, IntervalTrigger(seconds=settings.analyzer_run_interval),
                           id="analyzer", next_run_time=now, max_instances=1, misfire_grace_time=60)
        _scheduler.start()
        logger.info("Scheduler started with %d jobs", len(_scheduler.get_jobs()))
    except Exception as e:
        logger.error("Failed to start scheduler: %s", e, exc_info=True)

    yield

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _bot_app:
        await _bot_app.stop()
        await _bot_app.shutdown()
    logger.info("Shutting down...")


app = FastAPI(title="Weather Arbitrage Bot", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    jobs = [j.id for j in _scheduler.get_jobs()] if _scheduler else []
    return {"status": "ok", "service": "Weather Arbitrage Bot", "scheduler_jobs": jobs}


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not settings.telegram_webhook_secret or secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Forbidden")
    if _bot_app is None:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    data = await request.json()
    update = Update.de_json(data, _bot_app.bot)
    await _bot_app.process_update(update)
    return {"ok": True}


app.include_router(health.router, tags=["health"])
app.include_router(cities.router, prefix="/api/cities", tags=["cities"])
app.include_router(markets.router, prefix="/api/markets", tags=["markets"])
app.include_router(opportunities.router, prefix="/api/opportunities", tags=["opportunities"])
app.include_router(users.router, prefix="/api/users", tags=["users"])
