import logging

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.alert import TelegramUser
from app.models.city import City
from app.models.market import Market
from app.bot.formatters import fmt_status

logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TelegramUser).where(TelegramUser.chat_id == chat_id))
        user = result.scalar_one_or_none()
        if not user:
            user = TelegramUser(chat_id=chat_id, username=update.effective_user.username)
            db.add(user)
            await db.commit()
            msg = ("👋 Welcome to *Weather Arbitrage Bot*!\n\nCommands:\n/status — current market status\n/watch SF — start watching San Francisco\n/unwatch SF — stop watching\n/settings — configure alerts\n/dashboard — link to web dashboard\n")
        else:
            msg = "You're already registered! Use /status to see current markets."
    await update.message.reply_markdown(msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        city_signals = []
        for city in cities:
            from app.analyzers.signal_aggregator import SignalAggregator
            from app.models.market import MarketOutcome
            agg = SignalAggregator()
            outcome_result = await db.execute(select(MarketOutcome).join(Market).where(Market.city_id == city.id, Market.resolved == False).limit(1))
            outcome = outcome_result.scalar_one_or_none()
            if outcome:
                signals = await agg.aggregate(db, city.id, city.primary_icao, city.reference_icao, outcome)
                primary = signals.get("primary_metar") or {}
                wg = signals.get("wunderground_forecast") or {}
                city_signals.append({"city": city.name, "temp_f": primary.get("temperature_f"), "forecast_high": wg.get("predicted_high_f")})
    await update.message.reply_markdown(fmt_status(city_signals))


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /watch <city name or code>")
        return
    city_query = " ".join(context.args).upper()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        matched = [c for c in cities if city_query in c.name.upper() or city_query == c.primary_icao.upper()]
        if not matched:
            await update.message.reply_text(f"City '{city_query}' not found.")
            return
        city = matched[0]
        user_result = await db.execute(select(TelegramUser).where(TelegramUser.chat_id == chat_id))
        user = user_result.scalar_one_or_none()
        if not user:
            user = TelegramUser(chat_id=chat_id)
            db.add(user)
        watched = list(user.cities_watched or [])
        if city.id not in watched:
            watched.append(city.id)
            user.cities_watched = watched
            await db.commit()
            await update.message.reply_text(f"Now watching {city.name} 👁")
        else:
            await update.message.reply_text(f"Already watching {city.name}.")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <city name or code>")
        return
    city_query = " ".join(context.args).upper()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()
        matched = [c for c in cities if city_query in c.name.upper()]
        if not matched:
            await update.message.reply_text(f"City '{city_query}' not found.")
            return
        city = matched[0]
        user_result = await db.execute(select(TelegramUser).where(TelegramUser.chat_id == chat_id))
        user = user_result.scalar_one_or_none()
        if user and user.cities_watched and city.id in user.cities_watched:
            user.cities_watched = [c for c in user.cities_watched if c != city.id]
            await db.commit()
            await update.message.reply_text(f"Stopped watching {city.name}.")
        else:
            await update.message.reply_text(f"You weren't watching {city.name}.")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown("*Settings*\n\nTo change min confidence:\n`/setconf 70` (default: 60)")


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from app.config import settings
    base = settings.cors_origins_list[0] if settings.cors_origins_list else "http://localhost:3000"
    await update.message.reply_text(f"Dashboard: {base}")
