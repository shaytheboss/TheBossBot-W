import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import sentry_sdk
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from telegram import Update

from app.config import settings
from app.api import cities, markets, opportunities, users, health, admin
from app.utils.log_buffer import install_buffer_handler

logger = logging.getLogger(__name__)

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env)

_bot_app = None
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_app, _scheduler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_buffer_handler()
    logger.info("Weather Arbitrage Bot starting up...")

    try:
        from app.utils.seed import seed_cities
        summary = await seed_cities()
        logger.info(f"Startup seed: {summary}")
    except Exception as e:
        logger.error(f"Startup seed failed: {e}", exc_info=True)

    # Re-apply persisted admin-set thresholds BEFORE the scheduler starts, so
    # the first analyzer run already uses them (previously every restart
    # silently reverted to config defaults — e.g. alert threshold back to 0.75).
    try:
        from app.database import AsyncSessionLocal
        from app.utils.settings_store import load_setting_overrides
        async with AsyncSessionLocal() as db:
            await load_setting_overrides(db)
    except Exception as e:
        logger.error(f"Failed to load persisted settings: {e}", exc_info=True)

    if settings.telegram_bot_token:
        from app.bot.telegram_bot import get_app
        _bot_app = get_app()
        await _bot_app.initialize()
        await _bot_app.start()
        logger.info("Telegram bot initialized (webhook mode)")

    try:
        from app.workers.jobs import (
            job_discover_markets,
            job_fetch_metars, job_fetch_wunderground, job_fetch_nws,
            job_fetch_models, job_fetch_pireps, job_fetch_polymarket,
            job_run_analyzer, job_check_resolutions,
            job_fetch_external_forecasts, job_run_intraday,
        )
        from app.workers.icon_job import job_fetch_icon
        from app.workers.tomorrowio_job import job_fetch_tomorrowio
        now = datetime.now()
        _scheduler = AsyncIOScheduler()

        _scheduler.add_job(job_discover_markets, IntervalTrigger(seconds=1800),
                           id="discover", next_run_time=now, max_instances=1, misfire_grace_time=300)
        _scheduler.add_job(job_fetch_metars, IntervalTrigger(seconds=settings.metar_fetch_interval),
                           id="metar", next_run_time=now, max_instances=1, misfire_grace_time=60)
        _scheduler.add_job(job_fetch_wunderground, IntervalTrigger(seconds=settings.wunderground_fetch_interval),
                           id="wunderground", next_run_time=now, max_instances=1, misfire_grace_time=300)
        _scheduler.add_job(job_fetch_nws, IntervalTrigger(seconds=3600),
                           id="nws", next_run_time=now, max_instances=1, misfire_grace_time=300)
        _scheduler.add_job(job_fetch_models, IntervalTrigger(seconds=3600),
                           id="models", next_run_time=now, max_instances=1, misfire_grace_time=600)
        # ICON (DWD via Open-Meteo). The job existed since day one but was never
        # scheduled — collector_miss showed 100% no_data for all cities. Wired in
        # with its own id so it can be tracked separately from GFS/ECMWF.
        if getattr(settings, "icon_enabled", True):
            _scheduler.add_job(job_fetch_icon,
                               IntervalTrigger(seconds=getattr(settings, "icon_fetch_interval", 3600)),
                               id="icon", next_run_time=now, max_instances=1, misfire_grace_time=600)
        _scheduler.add_job(job_fetch_external_forecasts,
                           IntervalTrigger(seconds=settings.external_forecast_fetch_interval),
                           id="external_forecasts", next_run_time=now,
                           max_instances=1, misfire_grace_time=600)
        # Tomorrow.io runs on its own budget-aware hourly job (free tier allows
        # only 25 req/h, 500/day). The old shared external job burst 144 calls
        # at once and got rate-limited for every city except the first few.
        if settings.tomorrowio_api_key:
            _scheduler.add_job(job_fetch_tomorrowio,
                               IntervalTrigger(seconds=getattr(settings, "tomorrowio_fetch_interval", 3600)),
                               id="tomorrowio", next_run_time=now,
                               max_instances=1, misfire_grace_time=600)
        _scheduler.add_job(job_fetch_pireps, IntervalTrigger(seconds=900),
                           id="pireps", next_run_time=now, max_instances=1, misfire_grace_time=120)
        _scheduler.add_job(job_fetch_polymarket, IntervalTrigger(seconds=settings.polymarket_fetch_interval),
                           id="polymarket", next_run_time=now, max_instances=1, misfire_grace_time=60)
        _scheduler.add_job(job_run_analyzer, IntervalTrigger(seconds=settings.analyzer_run_interval),
                           id="analyzer", next_run_time=now, max_instances=1, misfire_grace_time=60)
        _scheduler.add_job(job_check_resolutions, IntervalTrigger(seconds=86400),
                           id="resolutions", next_run_time=now, max_instances=1, misfire_grace_time=3600)
        if getattr(settings, "intraday_enabled", True):
            _scheduler.add_job(job_run_intraday,
                               IntervalTrigger(seconds=getattr(settings, "intraday_run_interval", 300)),
                               id="intraday", next_run_time=now, max_instances=1, misfire_grace_time=60)

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

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    jobs = [j.id for j in _scheduler.get_jobs()] if _scheduler else []
    return {"status": "ok", "service": "Weather Arbitrage Bot", "scheduler_jobs": jobs}


@app.get("/admin")
async def admin_page():
    path = os.path.join(STATIC_DIR, "admin.html")
    if not os.path.isfile(path):
        raise HTTPException(404, "Admin page not found")
    return FileResponse(path)


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not settings.telegram_bot_token or secret != settings.telegram_webhook_secret:
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
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
