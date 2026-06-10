"""Empirical calibration: map raw certainty to historical win rates.

Queries settled opportunities (virtual_status win/loss) from the last
LOOKBACK_DAYS days, groups them into 2-point confidence bands, and computes
the empirical win rate per band.

The calibrated value blends the raw model certainty with the empirical rate,
weighted by how many samples are in the band (more data → stronger correction).
The result is stored in signals as _calibrated_confidence for display on the
dashboard; threshold decisions continue to use the raw certainty so the
virtual-buy tracking at 90–92% is not disrupted.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)

MIN_SAMPLES = 8         # below this, skip calibration for the band
LOOKBACK_DAYS = 120
MAX_BLEND_WEIGHT = 0.60  # empirical can move the estimate up to this far
CACHE_TTL_SECONDS = 1800

_cache: dict = {}                    # {band_lo: (empirical_win_rate, n)}
_cache_ts: Optional[datetime] = None


def _band(confidence_score: int) -> int:
    """Lower edge of the 2-point band, e.g. 91 → 90, 92 → 92."""
    return (confidence_score // 2) * 2


async def _load_table(db: AsyncSession) -> dict:
    from app.models.opportunity import Opportunity
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    result = await db.execute(
        select(Opportunity.confidence_score, Opportunity.virtual_status)
        .where(
            Opportunity.virtual_status.in_(["win", "loss"]),
            Opportunity.detected_at >= cutoff,
        )
    )
    rows = result.all()

    bands: dict[int, list[int]] = {}
    for row in rows:
        b = _band(row.confidence_score)
        entry = bands.setdefault(b, [0, 0])
        entry[1] += 1
        if row.virtual_status == "win":
            entry[0] += 1

    table = {}
    for b, (wins, total) in bands.items():
        if total >= MIN_SAMPLES:
            table[b] = (wins / total, total)
    return table


async def get_calibration_table(db: AsyncSession) -> dict:
    """Return cached calibration table, refreshing every 30 minutes."""
    global _cache, _cache_ts
    now = datetime.now(timezone.utc)
    if _cache_ts is not None and (now - _cache_ts).total_seconds() < CACHE_TTL_SECONDS:
        return _cache
    try:
        _cache = await _load_table(db)
        _cache_ts = now
        logger.debug(f"Calibration table loaded: {len(_cache)} bands")
    except Exception as exc:
        logger.warning(f"Calibration table load failed: {exc}")
    return _cache


def calibrate(raw_certainty: float, table: dict) -> float:
    """Blend raw certainty toward empirical win rate for its confidence band.

    When the band has no history or too few samples, returns raw_certainty
    unchanged. The blend weight grows with sample count but is capped at
    MAX_BLEND_WEIGHT so the model signal always carries meaningful weight.
    """
    if not table:
        return raw_certainty
    pct = int(raw_certainty * 100)
    b = _band(pct)
    entry = table.get(b)
    if entry is None:
        return raw_certainty
    empirical, n = entry
    weight = min(MAX_BLEND_WEIGHT, n / (n + 50.0))
    calibrated = raw_certainty * (1.0 - weight) + empirical * weight
    return round(calibrated, 4)
