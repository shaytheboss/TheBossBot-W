"""Paper trades on the nearby-station signal, priced at the live book.

Runs right after each nearby collection (every 5 minutes). For every city
with an open market inside its decision window it evaluates app/nearby/signal
on the stored METARs; the first time a market's signal fires with the
holding bucket's mid in band, it reads that bucket's book, records what a buy
would have cost (ask and the size there), and sends one Telegram message.
Later runs fill in the mid 10 and 30 minutes on. Nothing is bought.

Per run: one METAR read per due city, and a book read only when a signal
fires or a follow-up is due — a few dozen a day.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from html import escape
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.collectors.polymarket_collector import PolymarketCollector
from app.config import settings
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.metar import MetarObservation
from app.nearby import signal as sig
from app.nearby.job import in_window
from app.nearby.models import NearbySignal
from app.nearby.stations import NEARBY
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)

FOLLOW_UPS = (("mid_10", 10), ("mid_30", 30))
FOLLOW_UP_WINDOW = timedelta(hours=3)

#: What the paper test saw for each city, for the Nearby tab's status line — so
#: a day without rows can be told apart from a stuck job. One entry per city,
#: replaced when the city's local day changes. In memory only: no table, no
#: cost; a restart starts it again (STARTED says when).
TODAY: dict[str, dict] = {}
STARTED = datetime.now(timezone.utc)
TODAY_WINDOW = timedelta(hours=24)
_COUNTED = ("fired", "before_peak", "above_band", "below_band")


def _tally(city: City, day, now: datetime, stage: str, mid: Optional[float] = None) -> None:
    """Record one step of one check. `checked` counts the check; the counted
    stages add one each; any other stage is just the latest result."""
    rec = TODAY.get(city.name)
    if rec is None or rec["day"] != day:
        rec = TODAY[city.name] = {"day": day, "tz": city.timezone or "UTC", "checks": 0,
                                  **{k: 0 for k in _COUNTED}, "recorded": False, "bought": False,
                                  "last": None, "last_at": now, "last_mid": None}
    rec["last_at"] = now
    if stage == "checked":
        rec["checks"] += 1
        return
    if stage in _COUNTED:
        rec[stage] += 1
    if stage in ("recorded", "bought"):
        rec[stage] = True
    if stage != "fired":
        rec["last"] = stage
    if mid is not None:
        rec["last_mid"] = round(mid, 4)


def today_summary(now: Optional[datetime] = None) -> dict:
    """The last 24 hours, per city and in total, for the Nearby tab."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for name, r in sorted(TODAY.items()):
        if now - r["last_at"] > TODAY_WINDOW:
            continue
        rows.append({"city": name, "day": r["day"].isoformat(), "checks": r["checks"],
                     **{k: r[k] for k in _COUNTED}, "recorded": r["recorded"], "bought": r["bought"],
                     "last": r["last"], "last_mid": r["last_mid"],
                     "last_local": r["last_at"].astimezone(ZoneInfo(r["tz"])).strftime("%H:%M")})
    return {
        "since": STARTED.isoformat(timespec="minutes"),
        "cities_checked": len(rows),
        "cities_fired": sum(1 for r in rows if r["fired"]),
        "cities_before_peak": sum(1 for r in rows if r["before_peak"]),
        "cities_above_band": sum(1 for r in rows if r["above_band"]),
        "cities_below_band": sum(1 for r in rows if r["below_band"]),
        "cities_recorded": sum(1 for r in rows if r["recorded"]),
        "cities_bought": sum(1 for r in rows if r["bought"]),
        "rows": rows,
    }


def book_summary(book: Optional[dict]) -> Optional[dict]:
    """Best bid, best ask and the size offered at the ask. None unless two-sided."""
    if not book:
        return None
    try:
        bids = [(float(b["price"]), float(b.get("size") or 0)) for b in book.get("bids") or []]
        asks = [(float(a["price"]), float(a.get("size") or 0)) for a in book.get("asks") or []]
    except (TypeError, ValueError, KeyError):
        return None
    if not bids or not asks:
        return None
    bid = max(p for p, _ in bids)
    ask = min(p for p, _ in asks)
    if bid > ask:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2,
            "ask_size": sum(s for p, s in asks if p == ask)}


async def _readings(db, icaos: list[str], since: datetime, now: datetime) -> dict[str, list]:
    rows = (await db.execute(
        select(MetarObservation.icao, MetarObservation.observed_at, MetarObservation.temperature_f)
        .where(MetarObservation.icao.in_(icaos), MetarObservation.observed_at >= since,
               MetarObservation.observed_at <= now)
    )).all()
    out: dict[str, list] = {i: [] for i in icaos}
    for icao, at, temp in rows:
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        out.setdefault(icao, []).append((at, None if temp is None else float(temp)))
    return out


def _fmt(temp_f: float, unit: str) -> str:
    """A temperature in the market's own unit."""
    return f"{(temp_f - 32) * 5 / 9:.1f}°C" if unit == "C" else f"{temp_f:.1f}°F"


def _fmt_delta(delta_f: float, unit: str) -> str:
    """A temperature DIFFERENCE in the market's own unit (no -32)."""
    return f"{delta_f * 5 / 9:.1f}°C" if unit == "C" else f"{delta_f:.1f}°F"


def event_url(market: Market) -> Optional[str]:
    """The Polymarket event page — the same link the bot's other alerts use."""
    return f"https://polymarket.com/event/{market.external_id}" if market.external_id else None


def message(city: str, outcome: MarketOutcome, unit: str, v: sig.Verdict, book: dict,
            km: dict[str, float], link: Optional[str] = None,
            peak_share: Optional[float] = None) -> str:
    """The bet first, then its price, then why — everything in the market's unit."""
    fair = sig.fair_value(book["mid"]) if "mid" in book else None
    lines = [f"🟢 <b>Paper buy: YES {escape(outcome.bucket_label)}</b> — {escape(city)}",
             f"Ask <b>{book['ask'] * 100:.1f}¢</b> ({book['ask_size']:.0f} shares) · "
             f"bid {book['bid'] * 100:.1f}¢ · spread {(book['ask'] - book['bid']) * 100:.1f}¢"
             + (f" · worth ~{fair * 100:.0f}¢" if fair is not None else ""),
             f"High so far {_fmt(v.target.high_f, unit)} at {escape(v.target.icao)}, "
             f"now {_fmt(v.target.now_f, unit)}",
             f"<b>{len(v.falling)} of {len(v.nearby)}</b> nearby stations are past their own high:"]
    for s in sorted(v.nearby, key=lambda s: km.get(s.icao, 999)):
        mark = "↓" if s in v.falling else "↑"
        lines.append(f"  {mark} {escape(s.icao)} {km.get(s.icao, 0):.0f} km: "
                     f"{_fmt_delta(s.below_f, unit)} below its high")
    if peak_share is not None:
        lines.append(f"Past the usual peak: on {peak_share * 100:.0f}% of this month's days the high had come by now")
    if link:
        lines.append(f'<a href="{escape(link, quote=True)}">Open on Polymarket</a>')
    lines.append("<i>Paper only — nothing is bought. The bet: today's high stays in this bucket.</i>")
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
            logger.warning(f"[nearby-paper] Telegram send to {user.chat_id} failed: {e}")
    return sent


async def _follow_ups(db, now: datetime, collector: PolymarketCollector) -> int:
    done = 0
    open_rows = (await db.execute(
        select(NearbySignal, MarketOutcome.token_id)
        .join(MarketOutcome, MarketOutcome.id == NearbySignal.outcome_id)
        .where(NearbySignal.signaled_at >= now - FOLLOW_UP_WINDOW,
               (NearbySignal.mid_10.is_(None)) | (NearbySignal.mid_30.is_(None)))
    )).all()
    for row, token in open_rows:
        at = row.signaled_at if row.signaled_at.tzinfo else row.signaled_at.replace(tzinfo=timezone.utc)
        for field, minutes in FOLLOW_UPS:
            if getattr(row, field) is None and now - at >= timedelta(minutes=minutes):
                b = book_summary(await collector.get_orderbook(token)) if token else None
                if b:
                    setattr(row, field, b["mid"])
                    done += 1
                break          # one book read per row per run
    if done:
        await db.commit()
    return done


async def _peak_share(db, city_id: int, local: datetime) -> Optional[float]:
    """Share of this month's days whose high had come by this local time, from
    the peak guard; early in a month (too few days) the previous month's."""
    from app.peaks.guard import share_passed
    hour = local.hour + local.minute / 60
    share, _ = await share_passed(db, city_id, local.month, hour, today=local.date())
    if share is None:
        prev = 12 if local.month == 1 else local.month - 1
        share, _ = await share_passed(db, city_id, prev, hour, today=local.date())
    return share


async def run_paper(db, now: Optional[datetime] = None,
                    collector: Optional[PolymarketCollector] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    own = collector is None
    collector = collector or PolymarketCollector()
    stats = {"checked": 0, "fired": 0, "recorded": 0, "alerted": 0, "follow_ups": 0}
    try:
        cities = (await db.execute(select(City).where(City.active == True))).scalars().all()
        for city in cities:
            nb = NEARBY.get((city.primary_icao or "").upper())
            day = in_window(now, city.timezone) if nb else None
            if not nb or day is None:
                continue
            market = (await db.execute(select(Market).where(
                Market.city_id == city.id, Market.event_date == day, Market.resolved == False))).scalars().first()
            if market is None:
                continue
            existing = (await db.execute(
                select(NearbySignal).where(NearbySignal.market_id == market.id))).scalars().first()
            if existing is not None and existing.tradable:
                _tally(city, day, now, "done")
                continue                                    # one tradable signal per market
            # A signal met at an untradable book does NOT close the market: it is
            # re-checked every run until the book allows a buy (live since #136,
            # the first wide book had silenced the market for the whole day).
            stats["checked"] += 1
            _tally(city, day, now, "checked")
            midnight = datetime.combine(day, time(0), tzinfo=ZoneInfo(city.timezone or "UTC")).astimezone(timezone.utc)
            target_icao = city.primary_icao.upper()
            reads = await _readings(db, [target_icao] + [s for s, _, _ in nb], midnight, now)
            v = sig.evaluate(sig.station_state(target_icao, reads[target_icao], now),
                             [sig.station_state(s, reads.get(s, []), now) for s, _, _ in nb])
            if v is None or not v.fires:
                _tally(city, day, now, "no_reading" if v is None else "not_falling")
                continue
            local = now.astimezone(ZoneInfo(city.timezone or "UTC"))
            share = await _peak_share(db, city.id, local)
            if not sig.past_peak(share):
                stats["before_peak"] = stats.get("before_peak", 0) + 1
                _tally(city, day, now, "before_peak")
                continue                                    # the station may still be climbing
            stats["fired"] += 1
            _tally(city, day, now, "fired")
            outcomes = (await db.execute(select(MarketOutcome).where(MarketOutcome.market_id == market.id))).scalars().all()
            hold = next((o for o in outcomes if o.token_id and sig.holds(
                o.bucket_min, o.bucket_max, resolve_bucket_unit(o), v.target.high_f)), None)
            if hold is None:
                _tally(city, day, now, "no_bucket")
                continue
            book = book_summary(await collector.get_orderbook(hold.token_id))
            if book is None or not sig.mid_in_band(book["mid"]):
                # Not recorded: may still fire later in band. Above the band the
                # market already knew; that is counted, so a quiet day reads as one.
                stage = "no_book" if book is None else ("above_band" if book["mid"] > sig.MID_HI else "below_band")
                if stage != "no_book":
                    stats[stage] = stats.get(stage, 0) + 1
                _tally(city, day, now, stage, None if book is None else book["mid"])
                continue
            km = {s: d for s, d, _ in nb}
            tradable = sig.tradable(book["bid"], book["ask"])
            if existing is not None and not tradable:
                _tally(city, day, now, "waiting_buyable", book["mid"])
                continue                                    # keep the first sighting; try again next run
            fields = dict(
                outcome_id=hold.id, signaled_at=now, local_hour=round(local.hour + local.minute / 60, 2),
                max_f=v.target.high_f, current_f=v.target.now_f, n_nearby=len(v.nearby), n_falling=len(v.falling),
                detail=[{"icao": s.icao, "km": km.get(s.icao), "now_f": s.now_f, "high_f": s.high_f,
                         "below_f": round(s.below_f, 1)} for s in v.nearby],
                bid=book["bid"], ask=book["ask"], ask_size=book["ask_size"], tradable=tradable,
                mid_10=None, mid_30=None, alerted=False)
            if existing is None:
                row = NearbySignal(city_id=city.id, market_id=market.id, event_date=day, icao=target_icao,
                                   first_seen_at=now, **fields)
                db.add(row)
            else:                                           # the book now allows the buy
                row = existing
                for k, val in fields.items():
                    setattr(row, k, val)
            await db.commit()
            stats["recorded"] += 1
            _tally(city, day, now, "bought" if row.tradable else "recorded", book["mid"])
            if row.tradable:
                TODAY[city.name]["recorded"] = True
            if not row.tradable:
                stats["not_tradable"] = stats.get("not_tradable", 0) + 1
                continue                                    # recorded for the report, never announced
            if getattr(settings, "nearby_alerts_enabled", True):
                if await send_text(db, message(city.name, hold, resolve_bucket_unit(hold), v, book, km,
                                               link=event_url(market), peak_share=share)):
                    row.alerted = True
                    await db.commit()
                    stats["alerted"] += 1
        stats["follow_ups"] = await _follow_ups(db, now, collector)
    except Exception as e:   # research only: never let this reach trading
        stats["error"] = f"{type(e).__name__}: {e}"
        logger.warning(f"[nearby-paper] {stats['error']}")
        await db.rollback()
    finally:
        if own:
            await collector.close()
    return stats
