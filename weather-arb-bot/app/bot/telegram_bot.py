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


def _make_polymarket_url(slug: str, event_date) -> str:
    month = event_date.strftime("%B").lower()
    return f"https://polymarket.com/event/highest-temperature-in-{slug}-on-{month}-{event_date.day}-{event_date.year}"


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

    market_url = None
    if city and city.polymarket_slug and market.event_date:
        market_url = _make_polymarket_url(city.polymarket_slug, market.event_date)

    text = fmt_opportunity(
        city_name=city.name if city else "Unknown",
        market_question=market.question,
        bucket_label=outcome.bucket_label,
        market_price=float(opportunity.market_price),
        true_prob=float(opportunity.estimated_true_prob),
        edge=float(opportunity.edge),
        confidence=opportunity.confidence_score,
        signals=opportunity.signals or {},
        resolution_time=market.resolution_time,
        market_url=market_url,
    )

    users_result = await db.execute(select(TelegramUser).where(TelegramUser.min_confidence <= opportunity.confidence_score))
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        if user.cities_watched and city and city.id not in user.cities_watched:
            continue
        try:
            msg = await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
            alert = Alert(
                alert_type="OPPORTUNITY_DETECTED",
                city_id=city.id if city else None,
                market_id=market.id,
                opportunity_id=opportunity.id,
                priority="HIGH",
                message_text=text,
                telegram_message_id=msg.message_id,
            )
            db.add(alert)
        except Exception as e:
            logger.error(f"Failed to send alert to {user.chat_id}: {e}")
    opportunity.alert_sent = True
    await db.commit()
