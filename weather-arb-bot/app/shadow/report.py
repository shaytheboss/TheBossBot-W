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


def _pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


#: Hours before the close at which the winner's probability is shown. A fixed
#: grid instead of one row per snapshot: the first version printed up to 24
#: rows and nobody could read a story out of them.
CHECKPOINTS = (48, 24, 12, 6, 3, 1)
_ROW_LABEL_W = 10
_CELL_W = 5


def _at_checkpoint(hours: list[Hour], c: float) -> Optional[Hour]:
    """The snapshot closest to `c` hours before the close, within an hour."""
    near = [h for h in hours if abs(h.hours_to_close - c) <= 1.0]
    return min(near, key=lambda h: abs(h.hours_to_close - c)) if near else None


def _grid(hours: list[Hour], with_intraday: bool) -> list[str]:
    """Three short rows, one column per checkpoint, ASCII only, fixed width —
    nothing variable-length (bucket labels) goes inside it."""
    points = [_at_checkpoint(hours, c) for c in CHECKPOINTS]
    row = lambda name, cells: name.ljust(_ROW_LABEL_W) + "".join(x.rjust(_CELL_W) for x in cells)
    out = [row("hours left", [f"{c}h" for c in CHECKPOINTS]),
           row("daily", [_pct(p.model_win) if p else "-" for p in points])]
    if with_intraday:
        out.append(row("intraday", [_pct(p.intraday_win) if p else "-" for p in points]))
    out.append(row("market", [_pct(p.market_win) if p else "-" for p in points]))
    return out


def _favourites(hours: list[Hour], attr: str, labels: dict) -> Optional[str]:
    """How a side's favourite bucket moved: "A → B (from 12h left)"."""
    segs: list[tuple[int, float]] = []
    for h in hours:
        pick = getattr(h, attr)
        if pick is None:
            continue
        if not segs or segs[-1][0] != pick:
            segs.append((pick, h.hours_to_close))
    if not segs:
        return None
    parts = [html.escape(labels.get(pick, "?")) + ("" if i == 0 else f" (from {htc:.0f}h left)")
             for i, (pick, htc) in enumerate(segs)]
    if len(parts) > 4:
        parts = parts[:1] + ["…"] + parts[-2:]
    return " → ".join(parts)


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
    rows = list(rows)
    all_hours = by_hour(rows, winner_id)
    hours = [h for h in all_hours if h.hours_to_close > 0]
    after_close = len(all_hours) - len(hours)
    if not hours:
        return None

    winner = html.escape(labels.get(winner_id, "?"))
    intra_hours = [h for h in hours if h.intraday_pick is not None]
    lines = [
        f"🔬 <b>Shadow study</b> — {html.escape(city)} · {event_date.isoformat()}",
        f"Resolved: <b>{winner}</b> · tracked {hours[0].hours_to_close:.0f}h before close "
        f"({len(hours)} snapshot{'' if len(hours) == 1 else 's'})"
        + (f" · {after_close} after close left out" if after_close else ""),
    ]

    # Data problems first: a summary built on a broken record must say so
    # before it shows numbers that look like a result.
    seen = {r.outcome_id for r in rows if r.hours_to_close > 0}
    warnings = []
    if all(h.market_win is None and h.market_pick is None for h in hours):
        warnings.append("no market price was recorded for this market — the market side is empty")
    if len(labels) <= 2:
        warnings.append(f"only {len(labels)} bucket(s) of this market are in the database — "
                        "the model's numbers are not a full distribution")
    elif len(seen) < len(labels) // 2:
        warnings.append(f"only {len(seen)} of {len(labels)} buckets were ever recorded")
    lines += [f"⚠️ {w}" for w in warnings]

    lines += ["", f"<b>Chance given to the winner</b> ({winner})",
              "<pre>", *_grid(hours, bool(intra_hours)), "</pre>"]
    if len(labels) <= 2:
        # With one or two buckets every side "favours" the winner by default;
        # favourites, who-settled-first and scores would only mislead.
        return _cap("\n".join(lines))

    fav = [("daily", _favourites(hours, "model_pick", labels))]
    if intra_hours:
        fav.append(("intraday", _favourites(hours, "intraday_pick", labels)))
    fav.append(("market", _favourites(hours, "market_pick", labels)))
    lines.append("<b>Favourite bucket</b>")
    lines += [f"  {name}: {text or '—'}" for name, text in fav]

    sides = [("daily", knew_from(hours, winner_id, "model_pick")),
             ("market", knew_from(hours, winner_id, "market_pick"))]
    if intra_hours:
        sides.insert(1, ("intraday", knew_from(hours, winner_id, "intraday_pick")))
    settled = sorted([(v, n) for n, v in sides if v is not None], reverse=True)
    never = [n for n, v in sides if v is None]
    first = " · ".join(f"{n} {v:.0f}h before close" for v, n in settled)
    lines.append("<b>Settled on the winner</b>")
    lines.append("  " + " · ".join(x for x in (first, ", ".join(never) + " never" if never else "") if x))

    mb = sum(h.model_brier for h in hours) / len(hours)
    kb_vals = [h.market_brier for h in hours if h.market_brier is not None]
    lines += ["", "<b>Accuracy until the close</b> (Brier, lower = better)",
              f"  daily {mb:.3f} · market "
              + (f"{sum(kb_vals) / len(kb_vals):.3f}" if kb_vals else "—")]
    both = [h for h in intra_hours if h.market_brier is not None]
    if both:
        n = len(both)
        lines.append(f"  intraday hours only ({n}h): intraday "
                     f"{sum(h.intraday_brier for h in both) / n:.3f}"
                     f" · market {sum(h.market_brier for h in both) / n:.3f}")
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
