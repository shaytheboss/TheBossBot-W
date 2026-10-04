"""The forward test for model v2: would trading it against live prices have
made money, on markets settled since a chosen date?

Same rules as the analysis that motivated v2: live order book only (the ask
to buy YES, 1 - bid to buy NO), one entry per bucket at the first hour the
gap appears, in fixed windows before the close. Reads only the columns it
needs from shadow_snapshots joined to the outcome — an admin click, not a job.
"""
from __future__ import annotations

import math
from datetime import date

from sqlalchemy import select

from app.models.market import Market, MarketOutcome
from app.shadow.models import ShadowSnapshot

WINDOWS = (("morning of the event day (12-18 h before close)", 12, 18),
           ("1 day before (24-30 h)", 24, 30),
           ("2 days before (48-54 h)", 48, 54))
MARGINS = (0.05, 0.10, 0.20)


def _summary(pnls: list[tuple[int, float]]) -> dict:
    """pnls: (market_id, pnl per share). t is clustered by market."""
    n = len(pnls)
    if n == 0:
        return {"entries": 0}
    mean = sum(p for _, p in pnls) / n
    by_m: dict[int, list[float]] = {}
    for m, p in pnls:
        by_m.setdefault(m, []).append(p)
    g = len(by_m)
    t = None
    if g > 5:
        var = sum((sum(v) - len(v) * mean) ** 2 for v in by_m.values()) * g / (g - 1)
        se = math.sqrt(var) / n
        t = round(mean / se, 2) if se > 0 else None
    return {"entries": n, "markets": g, "pnl_per_share": round(mean, 4), "t": t,
            "total_per_share": round(sum(p for _, p in pnls), 2)}


async def v2_forward_report(db, since: date) -> dict:
    rows = (await db.execute(
        select(ShadowSnapshot.market_id, ShadowSnapshot.outcome_id, ShadowSnapshot.taken_at,
               ShadowSnapshot.hours_to_close, ShadowSnapshot.v2_p, ShadowSnapshot.model_p,
               ShadowSnapshot.bid, ShadowSnapshot.ask, MarketOutcome.won)
        .join(MarketOutcome, MarketOutcome.id == ShadowSnapshot.outcome_id)
        .join(Market, Market.id == ShadowSnapshot.market_id)
        .where(Market.resolved == True, ShadowSnapshot.event_date >= since,  # noqa: E712
               ShadowSnapshot.price_live == True, ShadowSnapshot.v2_p.isnot(None),  # noqa: E712
               ShadowSnapshot.bid.isnot(None), ShadowSnapshot.ask.isnot(None),
               MarketOutcome.won.isnot(None))
        .order_by(ShadowSnapshot.taken_at)
    )).all()
    out = {"since": since.isoformat(), "rows_considered": len(rows), "windows": {}}
    for label, lo, hi in WINDOWS:
        win = [r for r in rows if lo < r.hours_to_close <= hi and 0 < r.ask < 1]
        res = {}
        for margin in MARGINS:
            for model in ("v2", "current"):
                seen, pnls = set(), []
                for r in win:
                    p = r.v2_p if model == "v2" else r.model_p
                    won = 1.0 if r.won else 0.0
                    if p - r.ask > margin and (r.outcome_id, "Y") not in seen:
                        seen.add((r.outcome_id, "Y")); pnls.append((r.market_id, won - r.ask))
                    if (1 - p) - (1 - r.bid) > margin and (r.outcome_id, "N") not in seen:
                        seen.add((r.outcome_id, "N")); pnls.append((r.market_id, (1 - won) - (1 - r.bid)))
                res[f"{model}, edge>{margin:.2f}"] = _summary(pnls)
        out["windows"][label] = res
    out["note"] = ("pnl per share at live prices, one entry per bucket at the first hour the "
                   "gap appears; t is clustered by market. Judge v2 only on days after it "
                   "started recording, and only when t is well past 2 across windows.")
    return out
