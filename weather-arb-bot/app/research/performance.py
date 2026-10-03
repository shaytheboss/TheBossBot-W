"""Per-city results since a chosen date — the forward test.

Picking the best cities by looking back at results already seen cannot tell
skill from luck: with ~40 cities one always looks excellent. Fix the date,
then judge only trades after it. One GROUP BY per kind over settled trades;
no JSON, no per-row work.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timezone

from sqlalchemy import case, func, select

from app.models.city import City
from app.models.intraday import IntradayOpportunity
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity

MIN_TRADES = 20
#: With ~40 cities, z=2 turns up by chance in about one of them. A city is
#: called only past z=3.
Z_CLEAR = 3.0


def verdict(n: int, wins: int, avg_entry: float) -> tuple[float | None, str]:
    if n < MIN_TRADES:
        return None, f"too few trades (need {MIN_TRADES})"
    w = wins / n
    se = math.sqrt(max(w * (1 - w), 1e-9) / n)
    z = (w - avg_entry) / se
    if z >= Z_CLEAR:
        return z, "beats the price"
    if z <= -Z_CLEAR:
        return z, "loses to the price"
    return z, "not yet distinguishable from luck"


async def _by_city(db, model, since_dt) -> dict[str, dict]:
    rows = (await db.execute(
        select(City.name,
               func.count(model.id),
               func.sum(case((model.virtual_status == "win", 1), else_=0)),
               func.avg(model.virtual_entry_price),
               func.sum(model.virtual_pnl))
        .join(MarketOutcome, MarketOutcome.id == model.outcome_id)
        .join(Market, Market.id == MarketOutcome.market_id)
        .join(City, City.id == Market.city_id)
        .where(model.virtual_status.in_(("win", "loss")), model.detected_at >= since_dt)
        .group_by(City.name)
    )).all()
    out = {}
    for name, n, wins, entry, pnl in rows:
        n, wins, entry = int(n), int(wins or 0), float(entry or 0)
        z, label = verdict(n, wins, entry)
        out[name] = {"trades": n, "win_rate": round(wins / n, 3) if n else None,
                     "avg_entry": round(entry, 3), "edge_pp": round((wins / n - entry) * 100, 1) if n else None,
                     "pnl": round(float(pnl or 0), 2), "z": None if z is None else round(z, 2),
                     "verdict": label}
    return out


async def city_performance(db, since: date) -> dict:
    since_dt = datetime.combine(since, time(0), tzinfo=timezone.utc)
    daily = await _by_city(db, Opportunity, since_dt)
    intraday = await _by_city(db, IntradayOpportunity, since_dt)
    return {
        "since": since.isoformat(),
        "note": ("edge = win rate minus the average price paid. A city is called only "
                 f"past z={Z_CLEAR:g}: with ~40 cities, z=2 shows up by chance in about one."),
        "daily": dict(sorted(daily.items(), key=lambda kv: -kv[1]["pnl"])),
        "intraday": dict(sorted(intraday.items(), key=lambda kv: -kv[1]["pnl"])),
    }
