import logging
from datetime import date

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select, desc

from app.database import AsyncSessionLocal
from app.models.alert import TelegramUser
from app.models.city import City
from app.models.market import Market
from app.bot.formatters import fmt_status

logger = logging.getLogger(__name__)

# Forecast sources in priority order — wunderground rarely parses, GFS always works
_FORECAST_SOURCES = ["gfs", "ecmwf", "nws", "wunderground"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(TelegramUser).where(TelegramUser.chat_id == chat_id))
        user = result.scalar_one_or_none()
        if not user:
            user = TelegramUser(chat_id=chat_id, username=update.effective_user.username)
            db.add(user)
            await db.commit()
            msg = (
                "Welcome to *Weather Arbitrage Bot*!\n\n"
                "Commands:\n"
                "/status — all monitored stations\n"
                "/watch SF — watch a city\n"
                "/unwatch SF — stop watching\n"
                "/settings — configure alerts\n"
                "/dashboard — web dashboard link"
            )
        else:
            msg = "You're already registered! Use /status to see current stations."
    await update.message.reply_markdown(msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(City).where(City.active == True))
        cities = result.scalars().all()

        if not cities:
            await update.message.reply_text("No cities seeded yet. Contact admin.")
            return

        city_signals = []
        for city in cities:
            from app.models.metar import MetarObservation
            from app.models.forecast import Forecast

            metar_result = await db.execute(
                select(MetarObservation)
                .where(MetarObservation.icao == city.primary_icao)
                .order_by(desc(MetarObservation.observed_at))
                .limit(1)
            )
            metar = metar_result.scalar_one_or_none()

            # Try forecast sources in priority order; GFS/ECMWF always work
            forecast = None
            for source in _FORECAST_SOURCES:
                forecast_result = await db.execute(
                    select(Forecast)
                    .where(
                        Forecast.city_id == city.id,
                        Forecast.source == source,
                        Forecast.forecast_for_date == date.today(),
                    )
                    .limit(1)
                )
                forecast = forecast_result.scalar_one_or_none()
                if forecast:
                    break

            city_signals.append({
                "city": city.name,
                "icao": city.primary_icao,
                "temp_f": float(metar.temperature_f) if metar and metar.temperature_f else None,
                "forecast_high": forecast.predicted_high_f if forecast else None,
            })

    await update.message.reply_markdown(fmt_status(city_signals))


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /watch <city name or ICAO>")
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
            await update.message.reply_text(f"Now watching {city.name} ({city.primary_icao})")
        else:
            await update.message.reply_text(f"Already watching {city.name}.")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <city name or ICAO>")
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
    await update.message.reply_markdown(
        "*Settings*\n\nTo change min confidence:\n`/setconf 70` (default: 60)"
    )


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from app.config import settings
    base = settings.cors_origins_list[0] if settings.cors_origins_list else "http://localhost:3000"
    await update.message.reply_text(f"Dashboard: {base}")
