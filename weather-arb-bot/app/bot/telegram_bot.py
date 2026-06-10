import asyncio
import logging
import signal

from telegram import Bot
from telegram.ext import Application, CommandHandler

from app.config import settings
from app.bot.handlers import (
    cmd_start, cmd_status, cmd_scan,
    cmd_watch, cmd_unwatch, cmd_settings, cmd_dashboard,
)
from app.bot.formatters import fmt_opportunity

logger = logging.getLogger(__name__)

_app: Application | None = None


def get_app() -> Application:
    global _app
    if _app is None:
        _app = Application.builder().token(settings.telegram_bot_token).build()
        _app.add_handler(CommandHandler("start", cmd_start))
        _app.add_handler(CommandHandler("status", cmd_status))
        _app.add_handler(CommandHandler("scan", cmd_scan))
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
    from app.models.opportunity import Opportunity as Opp  # noqa: F401 (used below)

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

    # Use the actual event slug stored in external_id — guaranteed to match Polymarket's URL
    market_url = f"https://polymarket.com/event/{market.external_id}" if market.external_id else None

    # Look up the prior opportunity for UPDATE detection (stored in signals by detector)
    signals = opportunity.signals or {}
    prior_opportunity = None
    prior_opp_id = signals.get("_prior_opportunity_id")
    if prior_opp_id is not None:
        prior_result = await db.execute(select(Opp).where(Opp.id == prior_opp_id))
        prior_opportunity = prior_result.scalar_one_or_none()

    text = fmt_opportunity(
        city_name=city.name if city else "Unknown",
        market_question=market.question,
        bucket_label=outcome.bucket_label,
        market_price=float(opportunity.market_price),
        true_prob=float(opportunity.estimated_true_prob),
        edge=float(opportunity.edge),
        confidence=opportunity.confidence_score,
        signals=signals,
        side=opportunity.side or "YES",
        event_date=market.event_date,
        resolution_time=market.resolution_time,
        market_url=market_url,
        station_icao=city.primary_icao if city else None,
        city_timezone=city.timezone if city else None,
        prior_opportunity=prior_opportunity,
    )

    users_result = await db.execute(
        select(TelegramUser).where(TelegramUser.min_confidence <= opportunity.confidence_score)
    )
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


def _fmt_open_position_alert(alert: dict) -> str:
    conf = int(round(alert["certainty"] * 100))
    cal_conf = int(round(alert["calibrated_certainty"] * 100))
    edge_cents = int(round(alert["edge"] * 100))
    entry_cents = int(round(alert["entry_cost"] * 100))
    calib_note = f" (calibrated: {cal_conf}%)" if cal_conf != conf else ""
    change_note = alert.get("change_note")
    if change_note:
        headline = "🔁 *SIGNAL UPDATE — BUY ALREADY OPEN*"
        change_line = f"\n📊 *What changed:* {change_note}\n"
        footer = (
            "_The numbers moved materially since the last alert on this outcome. "
            "Not entering a new trade — a virtual buy is already open. "
            "Review if manually trading._"
        )
    else:
        headline = "🔁 *SIGNAL STILL FIRING — BUY ALREADY OPEN*"
        change_line = ""
        footer = (
            "_The signal has not changed direction. "
            "Monitor or consider scaling if manually trading._"
        )
    return (
        f"{headline}\n\n"
        f"📍 *{alert['city_name']}* — {alert['side']} on _{alert['bucket_label']}_\n"
        f"📅 {alert['event_date'].strftime('%b %d')}\n"
        f"{change_line}\n"
        f"Confidence: *{conf}%*{calib_note}  |  Edge: +{edge_cents}¢  |  Entry: {entry_cents}¢\n\n"
        f"A virtual buy is already open on this outcome today — no new position was created.\n"
        f"{footer}"
    )


def _fmt_bucket_switch_alert(alert: dict) -> str:
    conf = alert["new_confidence"]
    edge_cents = int(round(alert["new_edge"] * 100))
    new_entry = int(round(alert["new_entry_cost"] * 100))
    old_parts = []
    for label, entry in zip(alert["old_buckets"], alert["old_entry_prices"]):
        entry_cents = int(round(entry * 100))
        old_parts.append(f"• _{label}_ (opened at {entry_cents}¢)")
    old_str = "\n".join(old_parts) if old_parts else "• (unknown)"
    return (
        f"🔄 *BUCKET SWITCH SIGNAL*\n\n"
        f"📍 *{alert['city_name']}*\n"
        f"📅 {alert['event_date'].strftime('%b %d')}\n\n"
        f"*New signal:* {alert['new_side']} on _{alert['new_bucket_label']}_\n"
        f"Confidence: *{conf}%*  |  Edge: +{edge_cents}¢  |  Entry: {new_entry}¢\n\n"
        f"*Currently open position(s) on same market:*\n{old_str}\n\n"
        f"_Consider: exit current bucket(s) and enter the new one — or hold both if the "
        f"forecast is genuinely ambiguous between them._"
    )


async def send_side_alert(alert: dict, db) -> None:
    """Send a lightweight Telegram notification for open-position or bucket-switch signals."""
    if not settings.telegram_bot_token:
        return
    from sqlalchemy import select
    from app.models.alert import TelegramUser

    alert_type = alert.get("type")
    if alert_type == "open_position":
        text = _fmt_open_position_alert(alert)
        min_conf = int(round(alert["certainty"] * 100))
    elif alert_type == "bucket_switch":
        text = _fmt_bucket_switch_alert(alert)
        min_conf = alert["new_confidence"]
    else:
        logger.warning(f"send_side_alert: unknown type {alert_type!r}")
        return

    users_result = await db.execute(
        select(TelegramUser).where(TelegramUser.min_confidence <= min_conf)
    )
    users = users_result.scalars().all()
    bot = Bot(token=settings.telegram_bot_token)
    for user in users:
        try:
            await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send side alert to {user.chat_id}: {e}")
    await db.commit()
