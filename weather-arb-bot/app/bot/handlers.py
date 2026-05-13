import logging
from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select, desc

from app.database import AsyncSessionLocal
from app.models.alert import TelegramUser
from app.models.city import City
from app.bot.formatters import fmt_status

logger = logging.getLogger(__name__)

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
                "/scan — fetch Polymarket data + compare forecasts (no threshold)\n"
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

            # Collect ALL available forecasts for today from all sources
            forecasts_by_source = {}
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
                f = forecast_result.scalar_one_or_none()
                if f and f.predicted_high_f is not None:
                    forecasts_by_source[source] = f.predicted_high_f

            city_signals.append({
                "city": city.name,
                "icao": city.primary_icao,
                "temp_f": float(metar.temperature_f) if metar and metar.temperature_f else None,
                "forecasts": forecasts_by_source,
            })

    await update.message.reply_markdown(fmt_status(city_signals))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger Polymarket fetch + forecast comparison for ALL cities, no threshold filter."""
    await update.message.reply_text(
        "\U0001f50d Scanning… triggering market discovery + price fetch. "
        "This may take 30–60 seconds."
    )

    from app.workers.jobs import job_discover_markets, job_fetch_polymarket
    from app.models.market import Market, MarketOutcome
    from app.analyzers.signal_aggregator import SignalAggregator
    from app.analyzers.probability_estimator import estimate_true_probability
    from app.analyzers.confidence_scorer import compute_confidence

    try:
        await job_discover_markets()
        await job_fetch_polymarket()
    except Exception as e:
        logger.warning(f"cmd_scan data refresh error: {e}")

    aggregator = SignalAggregator()
    today = date.today()
    scan_dates = [today + timedelta(days=i) for i in range(3)]

    async with AsyncSessionLocal() as db:
        cities_result = await db.execute(select(City).where(City.active == True))
        all_cities = list(cities_result.scalars().all())
        cities_by_id = {c.id: c for c in all_cities}

        total_with_prices = 0
        total_no_prices = 0
        total_not_found = 0

        for target_date in scan_dates:
            markets_result = await db.execute(
                select(Market).where(
                    Market.resolved == False,
                    Market.event_date == target_date,
                )
            )
            day_markets = markets_result.scalars().all()
            city_ids_with_market = {m.city_id for m in day_markets}

            lines = [f"\U0001f4c5 *{target_date.strftime('%b %d')}*"]

            for city in all_cities:
                city_market = next((m for m in day_markets if m.city_id == city.id), None)

                if not city_market:
                    lines.append(
                        f"\U0001f4cd {city.name} `{city.primary_icao}`: ❌ not discovered"
                    )
                    total_not_found += 1
                    continue

                outcomes_result = await db.execute(
                    select(MarketOutcome).where(MarketOutcome.market_id == city_market.id)
                )
                outcomes = outcomes_result.scalars().all()

                if not outcomes:
                    lines.append(
                        f"\U0001f4cd {city.name} `{city.primary_icao}`: market found, no outcomes"
                    )
                    continue

                city_lines = [f"\U0001f4cd *{city.name}* `{city.primary_icao}`"]
                city_has_price = False

                for outcome in outcomes:
                    try:
                        signals = await aggregator.aggregate(
                            db=db,
                            city_id=city.id,
                            primary_icao=city.primary_icao,
                            reference_icao=city.reference_icao,
                            outcome=outcome,
                        )
                    except Exception as e:
                        logger.error(f"cmd_scan aggregation error for {city.name}: {e}")
                        continue

                    price_info = signals.get("market_price")
                    true_prob = estimate_true_probability(
                        signals, outcome.bucket_min, outcome.bucket_max
                    )
                    confidence = compute_confidence(
                        signals, outcome.bucket_min, outcome.bucket_max
                    )

                    label = outcome.bucket_label[:32]

                    if price_info:
                        city_has_price = True
                        total_with_prices += 1
                        yes_price = price_info["yes_price"]
                        edge = true_prob - yes_price
                        buy_flag = " ✅ BUY" if edge >= 0.10 and confidence >= 50 else ""
                        city_lines.append(
                            f"  • {label}: {round(yes_price*100)}¢ → "
                            f"est {round(true_prob*100)}% [{edge:+.0%}] conf:{confidence}{buy_flag}"
                        )
                    else:
                        total_no_prices += 1
                        token_hint = "no token_id" if not outcome.token_id else "no price yet"
                        city_lines.append(f"  • {label}: ⚠️ {token_hint}")

                lines.extend(city_lines)

            # Send one message per day to stay within Telegram's 4096-char limit
            text = "\n".join(lines)
            if len(text) > 3800:
                text = text[:3800] + "\n…(truncated)"
            try:
                await update.message.reply_markdown(text)
            except Exception as e:
                logger.error(f"cmd_scan send error: {e}")
                try:
                    await update.message.reply_text(text[:3800])
                except Exception:
                    pass

        summary = (
            f"✅ Scan complete — "
            f"{total_with_prices} outcomes with prices | "
            f"{total_no_prices} no price | "
            f"{total_not_found} markets not discovered"
        )
        await update.message.reply_text(summary)


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
