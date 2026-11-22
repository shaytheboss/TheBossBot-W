"""Record and announce the real-track signals the detectors noted this run.

Called by the analyzer job right after the alpha and beta detectors, in its
own session. For each bucket and side noted (the first note wins), unless the
real track already holds it: one row in realtrack_buys at the noted price
(a virtual buy of SHARES), then one Telegram message with its own header.

Per run: one read of realtrack_buys for the noted buckets, one small read per
new buy, and one read of the settled record when a message goes out — a few
a day. Reads the research positions; never writes them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.market import MarketOutcome
from app.models.opportunity import Opportunity
from app.realtrack import hook
from app.realtrack.models import RealTrackBuy

logger = logging.getLogger(__name__)

#: Same size as the research track's virtual buys, so dollar results compare.
SHARES = 5
#: A note older than this is not priced any more — skip it rather than record
#: a stale price (only happens if a run stalls between detection and here).
STALE = timedelta(minutes=20)

LAST_RUN: dict = {}


def event_url(slug: Optional[str]) -> Optional[str]:
    """The Polymarket event page — the same link the bot's other alerts use."""
    return f"https://polymarket.com/event/{slug}" if slug else None


def result_of(side: str, outcome_won: Optional[bool]) -> Optional[bool]:
    """Did a `side` share on this bucket win? None while the market is open."""
    if outcome_won is None:
        return None
    return bool(outcome_won) if side == "YES" else not bool(outcome_won)


async def track_record(db) -> dict:
    """Settled real-track buys so far: count, wins, average result per share."""
    rows = (await db.execute(
        select(RealTrackBuy.side, RealTrackBuy.price, MarketOutcome.won)
        .join(MarketOutcome, MarketOutcome.id == RealTrackBuy.outcome_id)
        .where(MarketOutcome.won.is_not(None))
    )).all()
    results = [(result_of(side, won), price) for side, price, won in rows]
    n = len(results)
    wins = sum(1 for won, _ in results if won)
    pnl = sum(int(won) - price for won, price in results)
    return {"settled": n, "wins": wins, "pnl_per_share": (pnl / n) if n else None}


def side_book(side: str, yes_bid: float, yes_ask: float) -> tuple[float, float]:
    """Bid and ask of the side being bought. A NO share's ask is 1 - the YES
    bid and its bid is 1 - the YES ask; the spread is the same either way."""
    if side == "YES":
        return yes_bid, yes_ask
    return round(1 - yes_ask, 4), round(1 - yes_bid, 4)


def message(n: dict, held: Optional[dict], record: dict) -> str:
    """The bet first, then its price, then why — distinct from every research alert.
    Prices are in the side being bought. `held` is the regular bot's own open
    position on this bucket and side, opened before this signal, if any."""
    c = lambda v: f"{v * 100:.1f}¢"    # noqa: E731
    when = "today" if n["lead"] == 0 else f"in {n['lead']} day{'s' if n['lead'] > 1 else ''}"
    hh = int(n["local_hour"])
    mm = int(round((n["local_hour"] - hh) * 60))
    bid, ask = side_book(n["side"], n["bid"], n["ask"])
    lines = [
        f"💰💰 <b>REAL TRACK — BUY {n['side']} {escape(n['bucket'])}</b>",
        f"{escape(n['city'])} · event {when} · {hh:02d}:{mm:02d} local",
        f"{n['side']} price <b>{c(n['price'])}</b> per share · "
        f"{n['side']} bid {c(bid)} · spread {c(ask - bid)}",
        f"Bot confidence <b>{n['confidence']}%</b> [{'α' if n['estimator'] == 'alpha' else 'β'}] · "
        f"rule: same day, ≥{hook.min_confidence()}%",
        "<i>Backtest May–Nov: this rule won 76% at ~73¢ — the bot's % runs high.</i>",
    ]
    if held:
        since = held["since"].astimezone(ZoneInfo(n.get("tz") or "UTC")).strftime("%d.%m")
        at = f" at {c(held['price'])}" if held.get("price") is not None else ""
        lines.append(f"📌 The regular bot already holds {n['side']} on this bucket since {since}{at} — "
                     "it records nothing new today; this is today's entry.")
    if record.get("settled"):
        lines.append(f"Real track so far: {record['wins']}/{record['settled']} won · "
                     f"{record['pnl_per_share'] * 100:+.1f}¢ per share")
    link = event_url(n.get("slug"))
    if link:
        lines.append(f'<a href="{escape(link, quote=True)}">Open on Polymarket</a>')
    lines.append(f"<i>Virtual buy of {SHARES} shares recorded at this price — 💰 Real track tab.</i>")
    return "\n".join(lines)


async def send_text(db, text: str) -> int:
    """Send to every subscriber over the bot's own Telegram channel (HTML).
    `Bot` is looked up on the telegram_bot module at call time, so the test
    harness's FakeBot covers this. Never raises."""
    if not settings.telegram_bot_token or not text:
        return 0
    from app.bot import telegram_bot as tb
    from app.models.alert import TelegramUser
    sent = 0
    bot = tb.Bot(token=settings.telegram_bot_token)
    for user in (await db.execute(select(TelegramUser))).scalars().all():
        try:
            await bot.send_message(chat_id=user.chat_id, text=text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            logger.warning(f"[realtrack] Telegram send to {user.chat_id} failed: {e}")
    return sent


async def _held_before(db, outcome_id: int, side: str, before: datetime) -> Optional[dict]:
    """The regular bot's earliest still-open position on this bucket and side
    opened BEFORE the signal, or None. A position it opens in the same run —
    seconds after the note, because its own gate (90%) is below ours — does
    not count: that is the same signal, not one held from before."""
    row = (await db.execute(
        select(Opportunity.detected_at, Opportunity.virtual_entry_price).where(
            Opportunity.outcome_id == outcome_id,
            Opportunity.side == side,
            Opportunity.virtual_status == "open",
            Opportunity.detected_at < before,
        ).order_by(Opportunity.detected_at).limit(1)
    )).first()
    if row is None:
        return None
    since = row.detected_at if row.detected_at.tzinfo else row.detected_at.replace(tzinfo=timezone.utc)
    return {"since": since, "price": None if row.virtual_entry_price is None else float(row.virtual_entry_price)}


async def process(db, now: Optional[datetime] = None) -> dict:
    noted = hook.drain()
    stats = {"noted": len(noted), "new": 0, "alerted": 0}
    if not noted or not getattr(settings, "realtrack_enabled", True):
        LAST_RUN.clear()
        LAST_RUN.update(stats)
        return stats
    now = now or hook._now()

    fresh: dict[tuple, dict] = {}
    for n in noted:
        if now - n["noted_at"] <= STALE:
            fresh.setdefault((n["outcome_id"], n["side"]), n)
    held = set()
    if fresh:
        held = {(o, s) for o, s in (await db.execute(
            select(RealTrackBuy.outcome_id, RealTrackBuy.side)
            .where(RealTrackBuy.outcome_id.in_({o for o, _ in fresh}))
        )).all()}

    new: list[tuple] = []
    for key, n in fresh.items():
        if key in held:
            continue
        held_before = await _held_before(db, n["outcome_id"], n["side"], n["noted_at"])
        row = RealTrackBuy(
            estimator=n["estimator"], city_id=n["city_id"], market_id=n["market_id"],
            outcome_id=n["outcome_id"], side=n["side"], event_date=n["event_date"],
            bought_at=n["noted_at"], local_hour=n["local_hour"], confidence=n["confidence"],
            bid=n["bid"], ask=n["ask"], price=n["price"], shares=SHARES,
            research_open=held_before is not None, alerted=False,
        )
        db.add(row)
        new.append((row, n, held_before))
    if not new:
        LAST_RUN.clear()
        LAST_RUN.update(stats)
        return stats
    try:
        await db.commit()
    except IntegrityError:
        # Another run recorded one of these first; the next run sees it held.
        await db.rollback()
        logger.warning("[realtrack] a bucket was recorded concurrently; skipped this run")
        LAST_RUN.clear()
        LAST_RUN.update(stats)
        return stats
    stats["new"] = len(new)

    if getattr(settings, "realtrack_alerts_enabled", True) and settings.telegram_bot_token:
        record = await track_record(db)
        for row, n, held_before in new:
            if await send_text(db, message(n, held_before, record)):
                row.alerted = True
                stats["alerted"] += 1
        await db.commit()
    logger.info(f"[realtrack] noted {stats['noted']}, new {stats['new']}, alerted {stats['alerted']}")
    LAST_RUN.clear()
    LAST_RUN.update(stats)
    return stats
