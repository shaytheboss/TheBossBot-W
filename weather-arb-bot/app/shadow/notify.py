"""Deliver shadow-study messages over the bot's existing Telegram channel.

Reuses the bot's `Bot` class and subscriber list rather than creating a second
integration. `Bot` is looked up on the telegram_bot module at call time, so the
test harness's FakeBot (which patches exactly that name) covers this path too.

HTML rather than the legacy Markdown the trading alerts use: these messages are
tables, and <pre> keeps the columns aligned where Markdown would not.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)


async def send_shadow_text(db, text: str) -> int:
    """Send to every subscriber. Returns how many deliveries succeeded; never
    raises — a Telegram outage must not fail the snapshot run."""
    if not settings.telegram_bot_token or not text:
        return 0
    from app.bot import telegram_bot as tb
    from app.models.alert import TelegramUser

    users = (await db.execute(select(TelegramUser))).scalars().all()
    if not users:
        return 0
    bot = tb.Bot(token=settings.telegram_bot_token)
    sent = 0
    for user in users:
        try:
            await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            logger.warning(f"[shadow] Telegram send to {user.chat_id} failed: {e}")
    return sent
