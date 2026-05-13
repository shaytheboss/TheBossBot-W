import logging
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from app.config import settings
from app.api import cities, markets, opportunities, users, health

logger = logging.getLogger(__name__)

if settings.sentry_dsn:
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env)

_bot_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Weather Arbitrage Bot starting up...")

    if settings.telegram_bot_token:
        from app.bot.telegram_bot import get_app
        _bot_app = get_app()
        await _bot_app.initialize()
        await _bot_app.start()
        logger.info("Telegram bot initialized (webhook mode)")

    yield

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
    return {"status": "ok", "service": "Weather Arbitrage Bot"}


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
