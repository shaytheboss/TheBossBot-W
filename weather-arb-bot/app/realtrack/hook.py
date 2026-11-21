"""The detectors' only touch-point with the real track.

`note()` is called by the alpha and beta detectors for every signal that got
past their price and threshold checks, BEFORE their research dedup. It keeps
the ones that meet the real-track rule in a plain in-memory list; the
analyzer job hands that list to `track.process()` right after the detectors
run. No database, no network, and every failure is swallowed: whatever
happens here, the detector carries on exactly as it would have.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

#: Signals noted since the last `drain()`. Plain values only — no ORM objects,
#: so nothing here depends on the detector's session staying open.
PENDING: list[dict] = []
#: A run notes a few dozen at most; the cap only matters if nothing drains.
MAX_PENDING = 500


def _now() -> datetime:
    """The clock — a function so tests can pin the city's local day."""
    return datetime.now(timezone.utc)


def local_lead(event_date: date, tz_name: Optional[str], now: datetime) -> int:
    """Days from the city's own today to the event (0 = the event is today there)."""
    local_today = now.astimezone(ZoneInfo(tz_name or "UTC")).date()
    return (event_date - local_today).days


def min_confidence() -> int:
    """The rule's floor as a whole percent (0.91 → 91), compared with the
    same rounded percent the bot stores as confidence_score."""
    return int(round(float(getattr(settings, "realtrack_min_confidence", 0.91)) * 100))


def qualifies(confidence: int, lead: int) -> bool:
    max_days = int(getattr(settings, "realtrack_max_days_ahead", 0))
    return 0 <= lead <= max_days and confidence >= min_confidence()


def note(*, estimator: str, city, market, outcome, side: str, certainty: float,
         book: dict, entry_cost: float, excluded: bool = False) -> bool:
    """Keep this signal if it meets the rule. Returns True when kept. Never raises."""
    try:
        if not getattr(settings, "realtrack_enabled", True) or excluded:
            return False
        confidence = int(round(float(certainty) * 100))
        if confidence < min_confidence():
            return False
        now = _now()
        lead = local_lead(market.event_date, city.timezone, now)
        if not qualifies(confidence, lead):
            return False
        local = now.astimezone(ZoneInfo(city.timezone or "UTC"))
        PENDING.append({
            "estimator": estimator,
            "city_id": city.id, "city": city.name, "tz": city.timezone,
            "market_id": market.id, "slug": market.external_id,
            "event_date": market.event_date, "lead": lead,
            "outcome_id": outcome.id, "bucket": outcome.bucket_label,
            "side": side, "confidence": confidence, "certainty": float(certainty),
            "bid": float(book["bid"]), "ask": float(book["ask"]),
            "price": float(entry_cost),
            "noted_at": now, "local_hour": round(local.hour + local.minute / 60, 2),
        })
        if len(PENDING) > MAX_PENDING:
            del PENDING[:-MAX_PENDING]
        return True
    except Exception as e:
        logger.warning(f"[realtrack] note skipped: {e}")
        return False


def drain() -> list[dict]:
    """Everything noted since the last call, oldest first; the list is emptied."""
    out = PENDING[:]
    PENDING.clear()
    return out


def reset() -> None:
    PENDING.clear()
