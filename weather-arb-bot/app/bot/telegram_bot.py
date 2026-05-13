import asyncio
import logging
import signal

from telegram import Bot
from telegram.ext import Application, CommandHandler

from app.config import settings
from app.bot.handlers import cmd_start, cmd_status, cmd_watch, cmd_unwatch, cmd_settings, cmd_dashboard
from app.bot.formatters import fmt_opportunity

logger = logging.getLogger(__name__)

_app: Application | None = None


def get_app() -> Application:
    global _app
    if _app is None:
        _app = Application.builder().token(settings.telegram_bot_token).build()
        _app.add_handler(CommandHandler("start", cmd_start))
        _app.add_handler(CommandHandler("status", cmd_status))
        _app.add_handler(CommandHandler("watch", cmd_watch))
        _app.add_handler(CommandHandler("unwatch", cmd_unwatch))
        _app.add_handler(CommandHandler("settings", cmd_settings))
        _app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    return _app


async def send_opportunity_alert(opportunity, db) -> None:
    if not settings.telegram_bot_token:
        return
    from sqlalchemy import select
    from app.models.alert import TelegramUser, Alert
    from app.models.market import MarketOutcome, Market
    from app.models.city import City

    outcome_result = await db.execute(select(MarketOutcome).where(MarketOutcome.id == opportunity.outcome_id))
    outcome = outcome_result.scalar_one_or_none()
    if not outcome:
        return
    market_result = await db.execute(select(Market).where(Market.id == outcome.market_id))
    market = market_result.scalar_one_or_none()
    if not market:
        return
    city_result = await db.execute(select(City).where(City.id == market.city_id))
    city = city_result.scalar_one_or_none()

    text = fmt_opportunity(city_name=city.name if city else "Unknown", market_question=market.question, bucket_label=outcome.bucket_label, market_price=float(opportunity.market_price), true_prob=float(opportunity.estimated_true_prob), edge=float(opportunity.edge), confidence=opportunity.confidence_score, signals=opportunity.signals or {}, resolution_time=market.resolution_time)

    users_result = await db.execute(select(TelegramUser).where(TelegramUser.min_confidence <= opportunity.confidence_score))
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        if user.cities_watched and city and city.id not in user.cities_watched:
            continue
        try:
            msg = await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
            alert = Alert(alert_type="OPPORTUNITY_DETECTED", city_id=city.id if city else None, market_id=market.id, opportunity_id=opportunity.id, priority="HIGH", message_text=text, telegram_message_id=msg.message_id)
            db.add(alert)
        except Exception as e:
            logger.error(f"Failed to send alert to {user.chat_id}: {e}")
    opportunity.alert_sent = True
    await db.commit()


async def main():
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set.")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = get_app()
    stop_event = asyncio.Event()

    def _stop(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if settings.is_production:
        await app.initialize()
        await app.start()
        await app.updater.start_webhook(listen="0.0.0.0", port=8443, url_path=f"/telegram/webhook/{settings.telegram_webhook_secret}", secret_token=settings.telegram_webhook_secret)
        await stop_event.wait()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    else:
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            await stop_event.wait()
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
