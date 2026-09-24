"""Turn a resolved market's hourly record into a readable story. Pure: no I/O.

The summary answers, for one market, the question the study exists for:

  who knew first?   the hour from which the model's favourite bucket was the
                    eventual winner and stayed so through close — and the same
                    for the market's favourite
  who was closer?   Brier score over the whole window, model vs market
                    (lower is better), and the average probability each gave
                    to the bucket that actually won

About missing rows. The snapshot skips a bucket when BOTH sides call it dead
(see snapshot.DEAD_MODEL_P / DEAD_MARKET_P), because those rows carry no
information and were most of the volume. Here a missing row reads as zero on
both sides — which is what it meant. If the winner was such a bucket, both
sides are charged the full miss, as they should be.
"""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

#: Telegram rejects messages over 4,096 characters.
TELEGRAM_LIMIT = 4096
#: Rows in the hour-by-hour table. Longer histories are sampled evenly, always
#: keeping the first hour and the final ones, where resolution is decided.
MAX_TABLE_ROWS = 24


@dataclass
class Row:
    taken_at: datetime
    hours_to_close: float
    local_hour: int
    outcome_id: int
    model_p: float
    market_p: Optional[float]


@dataclass
class Hour:
    taken_at: datetime
    hours_to_close: float
    local_hour: int
    model_win: float
    market_win: Optional[float]
    model_pick: Optional[int]
    market_pick: Optional[int]
    model_brier: float
    market_brier: Optional[float]


def _brier(rs: list[Row], winner_id: int, winner_missing: bool, get) -> float:
    """Multi-bucket Brier score for one hour. A winner with no row (both sides
    called it dead) is charged the full miss: predicted 0, happened 1."""
    total = sum((get(r) - (1.0 if r.outcome_id == winner_id else 0.0)) ** 2 for r in rs)
    return total + (1.0 if winner_missing else 0.0)


def by_hour(rows: Iterable[Row], winner_id: int) -> list[Hour]:
    """Collapse rows into one line per snapshot hour, oldest first."""
    grouped: dict[datetime, list[Row]] = defaultdict(list)
    for r in rows:
        grouped[r.taken_at].append(r)

    hours = []
    for t in sorted(grouped):
        rs = grouped[t]
        win = next((r for r in rs if r.outcome_id == winner_id), None)
        model_win = win.model_p if win else 0.0

        priced = [r for r in rs if r.market_p is not None]
        market_complete = len(priced) == len(rs)
        market_win = (win.market_p if win and win.market_p is not None
                      else (0.0 if win is None and market_complete else None))

        hours.append(Hour(
            taken_at=t,
            hours_to_close=rs[0].hours_to_close,
            local_hour=rs[0].local_hour,
            model_win=model_win,
            market_win=market_win,
            model_pick=max(rs, key=lambda r: r.model_p).outcome_id,
            market_pick=max(priced, key=lambda r: r.market_p).outcome_id if priced else None,
            model_brier=_brier(rs, winner_id, win is None, lambda r: r.model_p),
            market_brier=(_brier(rs, winner_id, win is None, lambda r: r.market_p)
                          if market_complete else None),
        ))
    return hours


def knew_from(hours: list[Hour], winner_id: int, attr: str) -> Optional[float]:
    """Hours-before-close from which the favourite was the winner and STAYED
    the winner to the end. None if it was not the winner at the last snapshot.

    "Stayed" matters: a side that flickered onto the right bucket two days out
    and then left it did not know anything useful.
    """
    since = None
    for h in reversed(hours):
        if getattr(h, attr) != winner_id:
            break
        since = h.hours_to_close
    return since


def _sample(hours: list[Hour], n: int = MAX_TABLE_ROWS) -> list[Hour]:
    if len(hours) <= n:
        return hours
    tail = 6                                   # the decisive final hours, always
    head = hours[:-tail]
    step = len(head) / (n - tail)
    picked = [head[int(i * step)] for i in range(n - tail)]
    return picked + hours[-tail:]


def _pct(v: Optional[float]) -> str:
    return "  —" if v is None else f"{v * 100:3.0f}%"


def build_summary(
    *,
    city: str,
    event_date: date,
    labels: dict[int, str],
    winner_id: int,
    rows: Iterable[Row],
) -> Optional[str]:
    """HTML for Telegram, or None if there is nothing to report."""
    hours = by_hour(rows, winner_id)
    if not hours:
        return None

    short = lambda oid: html.escape((labels.get(oid) or "?").replace("°F", "").replace("°C", "C"))
    lines = [
        f"🔬 <b>Shadow study</b> — {html.escape(city)} · {event_date.isoformat()}",
        f"Resolved: <b>{html.escape(labels.get(winner_id, '?'))}</b> · "
        f"tracked {hours[0].hours_to_close:.0f}h before close "
        f"({len(hours)} snapshot{'' if len(hours) == 1 else 's'})",
        "",
        "<pre>",
        " h-left local  model  mkt   model  mkt",
        "              →win   →win   pick   pick",
    ]
    for h in _sample(hours):
        lines.append(
            f"{h.hours_to_close:6.1f} {h.local_hour:02d}:00 {_pct(h.model_win)} {_pct(h.market_win)}"
            f"  {short(h.model_pick):>6} {short(h.market_pick) if h.market_pick else '—':>6}"
        )
    lines.append("</pre>")

    m_from = knew_from(hours, winner_id, "model_pick")
    k_from = knew_from(hours, winner_id, "market_pick")
    fmt = lambda v: "never settled on it" if v is None else f"from {v:.0f}h before close"
    lines += [
        "<b>Who knew first</b>",
        f"  model:  {fmt(m_from)}",
        f"  market: {fmt(k_from)}",
    ]
    if m_from is not None and (k_from is None or m_from > k_from + 0.5):
        lead = m_from - (k_from or 0.0)
        lines.append(f"  → model led by {lead:.0f}h")
    elif k_from is not None and (m_from is None or k_from > m_from + 0.5):
        lines.append(f"  → market led by {k_from - (m_from or 0.0):.0f}h")

    mb = sum(h.model_brier for h in hours) / len(hours)
    kb_vals = [h.market_brier for h in hours if h.market_brier is not None]
    mw = sum(h.model_win for h in hours) / len(hours)
    kw_vals = [h.market_win for h in hours if h.market_win is not None]
    lines += [
        "",
        "<b>Across the whole window</b>",
        f"  Brier (lower=better): model {mb:.3f} · market "
        + (f"{sum(kb_vals) / len(kb_vals):.3f}" if kb_vals else "—"),
        f"  avg prob. on winner:  model {mw * 100:.0f}% · market "
        + (f"{sum(kw_vals) / len(kw_vals) * 100:.0f}%" if kw_vals else "—"),
    ]
    return _cap("\n".join(lines))


def build_hourly_digest(
    *,
    taken_at: datetime,
    markets_tracked: int,
    gaps: list[tuple[str, str, float, float, float]],
    top: int = 8,
) -> str:
    """One message per hour, not one per market — a message per market would
    be ~150 Telegram messages an hour.

    gaps: (city, bucket label, hours_to_close, model_p, market_p)
    """
    lines = [
        f"🔬 <b>Shadow hourly</b> — {taken_at:%H:00} UTC · {markets_tracked} markets tracked",
    ]
    ranked = sorted(gaps, key=lambda g: abs(g[3] - g[4]), reverse=True)[:top]
    if not ranked:
        lines.append("No priced buckets this hour.")
        return _cap("\n".join(lines))
    lines += ["Largest model-vs-market gaps right now:", "<pre>"]
    for city, label, hrs, mp, kp in ranked:
        lines.append(
            f"{html.escape(city[:12]):12} {html.escape(label.replace('°F', ''))[:7]:>7} "
            f"{hrs:4.0f}h  model {mp * 100:3.0f}% mkt {kp * 100:3.0f}% ({(mp - kp) * 100:+.0f})"
        )
    lines.append("</pre>")
    return _cap("\n".join(lines))


def _cap(text: str) -> str:
    if len(text) <= TELEGRAM_LIMIT:
        return text
    # Never cut inside the <pre> block: an unclosed tag makes Telegram reject
    # the whole message rather than truncate it.
    cut = text[: TELEGRAM_LIMIT - 40]
    if cut.count("<pre>") > cut.count("</pre>"):
        cut += "\n…</pre>"
    return cut + "\n(truncated)"
