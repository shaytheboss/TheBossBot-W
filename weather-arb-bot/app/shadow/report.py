"""Turn a resolved market's hourly record into a readable story. Pure: no I/O.

The summary answers, for one market, the question the study exists for:

  who knew first?   the hour from which the model's favourite bucket was the
                    eventual winner and stayed so through close — and the same
                    for the market's favourite
  who was closer?   Brier score over the hours before the close, model vs market
                    (lower is better), and the average probability each gave
                    to the bucket that actually won

The intraday model (the one that sees the running max) is shown next to the
daily one for the hours it runs, and scored against the market over those
same hours only — the daily model cannot see the thermometer, so late in the
day it is no match for a market that can.

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
    intraday_p: Optional[float] = None


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
    intraday_win: Optional[float] = None
    intraday_pick: Optional[int] = None
    intraday_brier: Optional[float] = None


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
        # The intraday model estimates every bucket of a market or none.
        has_intra = all(r.intraday_p is not None for r in rs)

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
            intraday_win=((win.intraday_p if win else 0.0) if has_intra else None),
            intraday_pick=(max(rs, key=lambda r: r.intraday_p).outcome_id
                           if has_intra else None),
            intraday_brier=(_brier(rs, winner_id, win is None, lambda r: r.intraday_p)
                            if has_intra else None),
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
    return "-" if v is None else f"{v * 100:.0f}%"


#: (header line 1, header line 2) per column of the hour table.
_COLUMNS = (("hrs", "left"), ("local", ""), ("model", "win"), ("intra", "win"),
            ("mkt", "win"), ("model", "pick"), ("mkt", "pick"))


def _table(hours: list[Hour], label) -> list[str]:
    """The hour-by-hour table, every column right-aligned to one width taken
    from its header and its values together.

    ASCII only: an arrow or an em dash can be drawn wider than one cell in
    Telegram's monospace font, which is what pushed the columns out of line.
    Widths are measured on the raw text and HTML-escaped afterwards, since
    escaping changes the length ("<" is four characters) but not the width.
    """
    body = [(f"{h.hours_to_close:.1f}", f"{h.local_hour:02d}:00",
             _pct(h.model_win), _pct(h.intraday_win), _pct(h.market_win),
             label(h.model_pick), label(h.market_pick) if h.market_pick else "-")
            for h in hours]
    grid = [tuple(c[0] for c in _COLUMNS), tuple(c[1] for c in _COLUMNS)] + body
    widths = [max(len(row[i]) for row in grid) for i in range(len(_COLUMNS))]
    return [html.escape(" ".join(cell.rjust(w) for cell, w in zip(row, widths)))
            for row in grid]


def _raw_label(labels: dict, oid) -> str:
    return (labels.get(oid) or "?").replace("°F", "").replace("°C", "C")


def build_summary(
    *,
    city: str,
    event_date: date,
    labels: dict[int, str],
    winner_id: int,
    rows: Iterable[Row],
) -> Optional[str]:
    """HTML for Telegram, or None if there is nothing to report.

    Only snapshots taken before the city's day closed count. Rows after the
    close (recorded before the recorder stopped taking them) compare a
    forecast already looking at the next day with a price pinned at 0 or 100:
    they flipped "who knew first" and inflated the market's score.
    """
    all_hours = by_hour(rows, winner_id)
    hours = [h for h in all_hours if h.hours_to_close > 0]
    after_close = len(all_hours) - len(hours)
    if not hours:
        return None

    lines = [
        f"🔬 <b>Shadow study</b> — {html.escape(city)} · {event_date.isoformat()}",
        f"Resolved: <b>{html.escape(labels.get(winner_id, '?'))}</b> · "
        f"tracked {hours[0].hours_to_close:.0f}h before close "
        f"({len(hours)} snapshot{'' if len(hours) == 1 else 's'})"
        + (f" · {after_close} after close left out" if after_close else ""),
        "",
        "<pre>",
        *_table(_sample(hours), lambda oid: _raw_label(labels, oid)),
        "</pre>",
    ]

    m_from = knew_from(hours, winner_id, "model_pick")
    k_from = knew_from(hours, winner_id, "market_pick")
    intra_hours = [h for h in hours if h.intraday_pick is not None]
    fmt = lambda v: "never settled on it" if v is None else f"from {v:.0f}h before close"
    lines += [
        "<b>Who knew first</b>",
        f"  model:  {fmt(m_from)}",
        f"  market: {fmt(k_from)}",
    ]
    if intra_hours:
        i_from = knew_from(hours, winner_id, "intraday_pick")
        lines.append(f"  intraday: {fmt(i_from)}")
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
        "<b>Until the close</b>",
        f"  Brier (lower=better): model {mb:.3f} · market "
        + (f"{sum(kb_vals) / len(kb_vals):.3f}" if kb_vals else "—"),
        f"  avg prob. on winner:  model {mw * 100:.0f}% · market "
        + (f"{sum(kw_vals) / len(kw_vals) * 100:.0f}%" if kw_vals else "—"),
    ]
    both = [h for h in intra_hours if h.market_brier is not None]
    if both:
        n = len(both)
        lines += [
            "",
            f"<b>Intraday hours only</b> ({n}h, same hours for both)",
            f"  Brier: intraday {sum(h.intraday_brier for h in both) / n:.3f}"
            f" · market {sum(h.market_brier for h in both) / n:.3f}",
            f"  avg prob. on winner: intraday {sum(h.intraday_win for h in both) / n * 100:.0f}%"
            f" · market {sum(h.market_win for h in both) / n * 100:.0f}%",
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
