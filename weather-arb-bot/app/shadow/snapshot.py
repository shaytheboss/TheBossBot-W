"""The hourly shadow-study job: record, then report.

Runs at a fixed minute past every hour. Each run:

  1. For every open market within the trading horizon, computes the model's
     estimate for every bucket (reusing production code — see estimate.py)
     and records it beside the stored market price, tagged with the city's
     own clock.
  2. Prunes rows older than the retention window.
  3. Sends the post-resolution summary for any market that has settled since
     the last run — and, if the alert mode asks for it, an hourly digest.

Failure containment: each market commits on its own, and an error in one
rolls back only that market. Nothing here raises into the scheduler, and
nothing here can affect a trading decision.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import delete, select

from app.analyzers.opportunity_detector import _should_skip_market
from app.analyzers.signal_aggregator import SignalAggregator
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.shadow.clock import hour_floor, hours_to_close, local_hour
from app.shadow.estimate import estimate_market
from app.shadow.live_price import fetch_books, new_collector, price_job_age_min
from app.shadow.models import ShadowMarketState, ShadowSnapshot
from app.shadow.notify import send_shadow_text
from app.shadow.report import Row, build_hourly_digest, build_summary

logger = logging.getLogger(__name__)

#: A bucket is not recorded when BOTH sides call it dead. Most of a market's
#: eleven buckets sit here — the model near 0, the market at a cent or two —
#: and storing them hourly was most of the projected volume while carrying no
#: information about which side is right. The report reads a missing row as
#: zero on both sides, which is what it meant.
DEAD_MODEL_P = 0.02
DEAD_MARKET_P = 0.03

#: Alert modes the settings screen can choose between.
ALERT_MODES = ("off", "summary", "summary_hourly")

#: Summaries sent per run at most. Guards against a burst if many markets
#: resolve at once, or if Telegram was unconfigured for a while.
MAX_SUMMARIES_PER_RUN = 20

#: What the last run did, for the admin status view. In memory by design: the
#: status must not cost a query against the table it is describing.
LAST_RUN: dict = {}

_aggregator = SignalAggregator()


# The attributes the study reads from each ORM object. Copied into plain
# objects up front because a rollback expires EVERY instance in the session:
# after one market failed, the next market's `market.id` would try to reload
# lazily inside async code and raise, killing the rest of the run. With plain
# copies a rollback cannot reach them. (Caught by
# test_one_failing_market_does_not_stop_the_run.)
_MARKET_FIELDS = ("id", "city_id", "event_date", "question")
_CITY_FIELDS = ("id", "name", "primary_icao", "reference_icao", "nws_lat",
                "nws_lon", "timezone", "onshore_wind_dir")
_OUTCOME_FIELDS = ("id", "market_id", "bucket_label", "bucket_min", "bucket_max",
                   "bucket_unit", "token_id")


def _plain(obj, fields) -> SimpleNamespace:
    return SimpleNamespace(**{f: getattr(obj, f, None) for f in fields})


def is_dead(model_p: float, market_p: Optional[float],
            intraday_p: Optional[float] = None) -> bool:
    """Every side that has a view calls the bucket dead. The intraday model
    counts: late in the day it can revive a bucket the daily model dismissed."""
    return (model_p < DEAD_MODEL_P
            and (market_p is None or market_p < DEAD_MARKET_P)
            and (intraday_p is None or intraday_p < DEAD_MODEL_P))


async def job_shadow_snapshot(
    session_factory: Optional[Callable] = None,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
) -> dict:
    """Entry point for the scheduler. Arguments exist only for tests."""
    if not getattr(settings, "shadow_enabled", True):
        return {"enabled": False}

    factory = session_factory or AsyncSessionLocal
    now = now or datetime.now(timezone.utc)
    today = today or date.today()
    hour = hour_floor(now)
    started = time.monotonic()
    stats = {"at": hour.isoformat(), "markets": 0, "rows": 0, "skipped_dead": 0,
             "book_calls": 0, "live_prices": 0, "stored_fallbacks": 0,
             "price_job_age_min": price_job_age_min(),
             "already_done": 0, "no_forecast": 0, "errors": 0,
             "summaries_sent": 0, "digest_sent": False, "pruned": 0,
             "past_close": 0}
    gaps: list[tuple] = []

    collector = new_collector()
    try:
        async with factory() as db:
            await _record(db, now, today, hour, stats, gaps, collector)
            stats["pruned"] = await _prune(db, now)

            mode = str(getattr(settings, "shadow_alert_mode", "summary") or "summary")
            if mode in ("summary", "summary_hourly"):
                stats["summaries_sent"] = await send_pending_summaries(db, now)
            if mode == "summary_hourly" and stats["markets"]:
                text = build_hourly_digest(
                    taken_at=hour, markets_tracked=stats["markets"], gaps=gaps
                )
                stats["digest_sent"] = (await send_shadow_text(db, text)) > 0
    except Exception as e:
        stats["errors"] += 1
        stats["fatal"] = f"{type(e).__name__}: {e}"
        logger.error(f"[shadow] run failed: {e}", exc_info=True)
    finally:
        await collector.close()

    stats["duration_s"] = round(time.monotonic() - started, 2)
    LAST_RUN.clear()
    LAST_RUN.update(stats)
    logger.info(
        f"[shadow] {stats['markets']} markets, {stats['rows']} rows "
        f"({stats['skipped_dead']} dead buckets skipped), "
        f"{stats['summaries_sent']} summaries, {stats['duration_s']}s"
    )
    return stats


async def _record(db, now, today, hour, stats, gaps, collector) -> None:
    horizon = int(getattr(settings, "shadow_max_days_ahead",
                          getattr(settings, "max_days_ahead_for_alert", 3)))
    markets = (await db.execute(
        select(Market).where(
            Market.resolved == False,  # noqa: E712 — SQL predicate, not a Python test
            Market.event_date >= today,
            Market.event_date <= today + timedelta(days=horizon),
        ).order_by(Market.event_date)
    )).scalars().all()
    markets = [_plain(m, _MARKET_FIELDS) for m in markets
               if not _should_skip_market(m.question)]
    if not markets:
        return
    ids = [m.id for m in markets]

    # Three bulk reads instead of three per market.
    done = set((await db.execute(
        select(ShadowSnapshot.market_id).where(
            ShadowSnapshot.taken_at == hour, ShadowSnapshot.market_id.in_(ids)
        ).distinct()
    )).scalars().all())
    cities = {c.id: _plain(c, _CITY_FIELDS)
              for c in (await db.execute(select(City))).scalars().all()}
    outcomes_by_market: dict[int, list] = defaultdict(list)
    for o in (await db.execute(
        select(MarketOutcome).where(MarketOutcome.market_id.in_(ids)).order_by(MarketOutcome.id)
    )).scalars().all():
        outcomes_by_market[o.market_id].append(_plain(o, _OUTCOME_FIELDS))
    seen = set((await db.execute(
        select(ShadowMarketState.market_id).where(ShadowMarketState.market_id.in_(ids))
    )).scalars().all())

    for market in markets:
        if market.id in done:
            stats["already_done"] += 1
            continue
        city = cities.get(market.city_id)
        outcomes = outcomes_by_market.get(market.id) or []
        if city is None or not outcomes:
            continue
        # The city's day is over: the answer is decided and the market only
        # waits for Polymarket to settle it. A snapshot now compares a forecast
        # for a day that has ended — after local midnight the aggregator is
        # already looking at the next day — with a price that is 0 or 100.
        # Checked before the estimate so it costs no queries either.
        if hours_to_close(now, market.event_date, city.timezone) <= 0:
            stats["past_close"] += 1
            continue
        try:
            ests = await estimate_market(db, city, market, outcomes, now,
                                         aggregator=_aggregator, today=today)
            if not ests:
                stats["no_forecast"] += 1
                continue

            htc = hours_to_close(now, market.event_date, city.timezone)
            lh = local_hour(now, city.timezone)
            labels = {o.id: o.bucket_label for o in outcomes}
            tokens = {o.id: o.token_id for o in outcomes}

            # Live order book, only for buckets not already dead on both sides
            # by the stored price — dead buckets are never worth a request.
            candidates = [e for e in ests if not is_dead(e.model_p, e.market_p, e.intraday_p)]
            stats["skipped_dead"] += len(ests) - len(candidates)
            wanted = [tokens[e.outcome_id] for e in candidates if tokens.get(e.outcome_id)]
            books = await fetch_books(wanted, collector)
            stats["book_calls"] += len(wanted)

            best_gap = None
            for e in candidates:
                book = books.get(tokens.get(e.outcome_id) or "")
                if book:
                    market_p, bid, ask, live = book["mid"], book["bid"], book["ask"], True
                    stats["live_prices"] += 1
                else:
                    market_p, bid, ask, live = e.market_p, None, None, False
                    stats["stored_fallbacks"] += 1
                if is_dead(e.model_p, market_p, e.intraday_p):
                    # The live price moved it into the dead zone after all.
                    stats["skipped_dead"] += 1
                    continue
                db.add(ShadowSnapshot(
                    outcome_id=e.outcome_id, taken_at=hour, market_id=market.id,
                    city_id=city.id, event_date=market.event_date,
                    hours_to_close=htc, local_hour=lh,
                    model_p=e.model_p, raw_p=e.raw_p, normalized=e.normalized,
                    market_p=market_p, bid=bid, ask=ask, price_live=live,
                    price_job_age_min=stats["price_job_age_min"],
                    n_sources=e.n_sources,
                    forecast_age_min=e.forecast_age_min,
                    forecast_high_f=e.forecast_high_f, sigma=e.sigma,
                    intraday_p=e.intraday_p,
                ))
                stats["rows"] += 1
                if market_p is not None:
                    g = (city.name, labels.get(e.outcome_id, "?"), htc, e.model_p, market_p)
                    if best_gap is None or abs(g[3] - g[4]) > abs(best_gap[3] - best_gap[4]):
                        best_gap = g
            if best_gap:
                gaps.append(best_gap)
            if market.id not in seen:
                db.add(ShadowMarketState(market_id=market.id, first_seen_at=hour))
                seen.add(market.id)
            await db.commit()
            stats["markets"] += 1
        except Exception as e:
            stats["errors"] += 1
            await db.rollback()
            logger.warning(f"[shadow] market {market.id} skipped: {e}", exc_info=True)


async def _prune(db, now) -> int:
    days = int(getattr(settings, "shadow_retention_days", 45))
    cutoff = now - timedelta(days=days)
    try:
        n = (await db.execute(
            delete(ShadowSnapshot).where(ShadowSnapshot.taken_at < cutoff)
        )).rowcount or 0
        await db.execute(
            delete(ShadowMarketState).where(ShadowMarketState.first_seen_at < cutoff)
        )
        await db.commit()
        return int(n)
    except Exception as e:
        await db.rollback()
        logger.warning(f"[shadow] prune failed: {e}")
        return 0


async def send_pending_summaries(db, now: datetime) -> int:
    """Post-resolution summaries, each sent exactly once.

    A market qualifies once it is marked resolved AND a winning bucket is
    recorded — resolution is written by the daily job_check_resolutions, so
    summaries arrive after that run, not the instant the day ends.
    """
    if not settings.telegram_bot_token:
        # Not marked as sent: they go out once Telegram is configured.
        return 0
    pending = (await db.execute(
        select(ShadowMarketState, Market)
        .join(Market, Market.id == ShadowMarketState.market_id)
        .where(ShadowMarketState.summary_sent_at.is_(None),
               Market.resolved == True)  # noqa: E712
        .order_by(Market.event_date)
        .limit(MAX_SUMMARIES_PER_RUN)
    )).all()

    sent = 0
    for state, market in pending:
        outcomes = (await db.execute(
            select(MarketOutcome).where(MarketOutcome.market_id == market.id)
        )).scalars().all()
        winner = next((o for o in outcomes if o.won is True), None)
        if winner is None:
            continue          # resolved, but the winning bucket is not recorded yet
        city = await db.get(City, market.city_id)
        snaps = (await db.execute(
            select(ShadowSnapshot).where(ShadowSnapshot.market_id == market.id)
            .order_by(ShadowSnapshot.taken_at)
        )).scalars().all()
        text = build_summary(
            city=city.name if city else "?",
            event_date=market.event_date,
            labels={o.id: o.bucket_label for o in outcomes},
            winner_id=winner.id,
            rows=[Row(s.taken_at, s.hours_to_close, s.local_hour, s.outcome_id,
                      s.model_p, s.market_p, s.intraday_p) for s in snaps],
        )
        if text and await send_shadow_text(db, text):
            sent += 1
        # Marked even when there was nothing to say, so an empty market is not
        # re-examined every hour forever.
        state.summary_sent_at = now
        await db.commit()
    return sent
