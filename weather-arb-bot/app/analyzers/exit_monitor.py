"""Virtual stop-loss / exit monitor for open beta positions.

Completely isolated from all existing virtual-position logic. It:
  1. Loads all beta Opportunity rows with virtual_status="open".
  2. Re-runs the beta estimator with FRESH forecasts for each position's outcome.
  3. If the fresh estimate diverges materially from the entry estimate, records a
     VirtualExit row and sends a prominent Telegram alert.

Nothing in the original Opportunity row is ever modified.

Trigger conditions (both must be true):
  - Confidence drop: fresh certainty dropped >= EXIT_CONFIDENCE_DROP_PP percentage
    points from entry (e.g. 90% → 70% = 20pp drop).
  - Forecast shift: the consensus forecast high shifted >= EXIT_FORECAST_SHIFT_F
    degrees from the forecast stored in signals["_beta_breakdown"]["forecast_high_f"].

Either condition alone can trigger when extreme:
  - Certainty collapses below EXIT_CERTAINTY_FLOOR regardless of forecast shift.
  - Forecast shifts >= EXIT_FORECAST_SHIFT_EXTREME_F regardless of confidence.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.analyzers.beta_opportunity_detector import (
    _load_city_skill,
    _beta_collect_outcome_data,
)
from app.analyzers.beta_estimator import beta_blend_with_market
from app.collectors.polymarket_collector import PolymarketCollector
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity
from app.models.virtual_exit import VirtualExit
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)

# ── Trigger thresholds ─────────────────────────────────────────────────────────

# Primary dual trigger: both conditions required together.
EXIT_CONFIDENCE_DROP_PP = 20      # percentage points drop in certainty (e.g. 90 → 70)
EXIT_FORECAST_SHIFT_F = 2.0       # °F shift in consensus forecast high

# Single-condition extremes: either alone triggers an exit.
EXIT_CERTAINTY_FLOOR = 0.55       # certainty collapses below this → always exit
EXIT_FORECAST_SHIFT_EXTREME_F = 5.0  # forecast shifts this much → always exit

_exit_poly_col = PolymarketCollector()


@dataclass
class ExitCandidate:
    opportunity: Opportunity
    fresh_certainty: float
    entry_certainty: float
    entry_forecast_high_f: Optional[float]
    fresh_forecast_high_f: Optional[float]
    forecast_shift_f: float
    confidence_drop_pp: float
    trigger_reason: str
    theoretical_exit_price: Optional[float]
    signals_at_exit: dict


def _extract_entry_forecast_high(opp: Opportunity) -> Optional[float]:
    """Pull the forecast high stored when the position was opened."""
    try:
        breakdown = (opp.signals or {}).get("_beta_breakdown", {})
        v = breakdown.get("forecast_high_f")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _extract_entry_certainty(opp: Opportunity) -> float:
    """Reconstruct entry certainty from confidence_score."""
    return (opp.confidence_score or 0) / 100.0


def _should_trigger_exit(
    entry_certainty: float,
    fresh_certainty: float,
    forecast_shift_f: float,
) -> tuple[bool, str]:
    """Return (should_exit, reason_string)."""
    confidence_drop_pp = (entry_certainty - fresh_certainty) * 100.0

    # Floor-breach: certainty has collapsed regardless of forecast shift.
    if fresh_certainty < EXIT_CERTAINTY_FLOOR:
        return True, (
            f"certainty collapsed {entry_certainty*100:.0f}% → {fresh_certainty*100:.0f}% "
            f"(below floor {EXIT_CERTAINTY_FLOOR*100:.0f}%)"
        )

    # Extreme forecast shift alone.
    if abs(forecast_shift_f) >= EXIT_FORECAST_SHIFT_EXTREME_F:
        return True, (
            f"forecast shifted {forecast_shift_f:+.1f}°F "
            f"(extreme threshold ±{EXIT_FORECAST_SHIFT_EXTREME_F:.0f}°F)"
        )

    # Primary dual trigger.
    if (
        confidence_drop_pp >= EXIT_CONFIDENCE_DROP_PP
        and abs(forecast_shift_f) >= EXIT_FORECAST_SHIFT_F
    ):
        return True, (
            f"confidence dropped {confidence_drop_pp:.0f}pp "
            f"({entry_certainty*100:.0f}% → {fresh_certainty*100:.0f}%) "
            f"AND forecast shifted {forecast_shift_f:+.1f}°F"
        )

    return False, ""


async def _get_theoretical_exit_price(
    opp: Opportunity,
    outcome: MarketOutcome,
) -> Optional[float]:
    """Fetch current book and return the price we'd exit at."""
    if not outcome.token_id:
        return None
    try:
        book = await _exit_poly_col.get_book_summary(outcome.token_id)
        if book is None:
            return None
        # For a YES position we sell at bid; for NO position we sell at (1-ask).
        if opp.side == "YES":
            return float(book["bid"]) if book["bid"] > 0 else None
        else:
            ask = float(book["ask"]) if book["ask"] > 0 else None
            return round(1.0 - ask, 4) if ask is not None else None
    except Exception as e:
        logger.debug(f"[exit_monitor] Book fetch failed for outcome {outcome.id}: {e}")
        return None


async def check_open_positions(db: AsyncSession) -> List[VirtualExit]:
    """Scan all open beta positions and record exits where warranted.

    Returns the list of VirtualExit rows just written to the DB.
    Never raises — all exceptions are caught internally.
    """
    exits_created: List[VirtualExit] = []

    try:
        result = await db.execute(
            select(Opportunity).where(
                Opportunity.estimator == "beta",
                Opportunity.virtual_status == "open",
            )
        )
        open_opps: List[Opportunity] = result.scalars().all()
    except Exception as e:
        logger.error(f"[exit_monitor] Failed to load open positions: {e}", exc_info=True)
        return exits_created

    if not open_opps:
        return exits_created

    logger.info(f"[exit_monitor] Checking {len(open_opps)} open beta positions.")

    for opp in open_opps:
        try:
            exit_row = await _check_single_position(db, opp)
            if exit_row is not None:
                exits_created.append(exit_row)
        except Exception as e:
            logger.error(
                f"[exit_monitor] Error checking opp {opp.id}: {e}", exc_info=True
            )

    return exits_created


async def _check_single_position(
    db: AsyncSession,
    opp: Opportunity,
) -> Optional[VirtualExit]:
    """Re-evaluate one open position. Returns a VirtualExit if exit triggered."""

    # ── Load the related market/outcome/city ────────────────────────────────────
    outcome_result = await db.execute(
        select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id)
    )
    outcome: Optional[MarketOutcome] = outcome_result.scalar_one_or_none()
    if outcome is None:
        return None

    market_result = await db.execute(
        select(Market).where(Market.id == outcome.market_id)
    )
    market: Optional[Market] = market_result.scalar_one_or_none()
    if market is None or market.resolved:
        return None

    # Skip markets that have already passed.
    if market.event_date < date.today():
        return None

    city_result = await db.execute(select(City).where(City.id == market.city_id))
    city: Optional[City] = city_result.scalar_one_or_none()
    if city is None:
        return None

    # ── Check whether we already recorded an exit for this position today ───────
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )
    existing = await db.execute(
        select(VirtualExit.id).where(
            VirtualExit.opportunity_id == opp.id,
            VirtualExit.triggered_at >= today_start,
        ).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return None  # Already alerted today.

    # ── Re-run beta signal collection with fresh forecasts ─────────────────────
    days_ahead = (market.event_date - date.today()).days
    city_lat = float(city.nws_lat) if city.nws_lat is not None else None
    city_lon = float(city.nws_lon) if city.nws_lon is not None else None

    try:
        city_skill = await _load_city_skill(db, city.id, days_ahead)
    except Exception as e:
        logger.warning(f"[exit_monitor] _load_city_skill failed for city {city.id}: {e}")
        city_skill = {}

    try:
        data = await _beta_collect_outcome_data(
            db=db,
            city=city,
            outcome=outcome,
            market=market,
            city_lat=city_lat,
            city_lon=city_lon,
            days_ahead=days_ahead,
            city_skill=city_skill,
        )
    except Exception as e:
        logger.warning(f"[exit_monitor] Data collection failed for opp {opp.id}: {e}")
        return None

    if data is None:
        return None

    fresh_breakdown = data["breakdown"]
    fresh_raw_prob = data["raw_prob"]

    # Blend with market price to get the same final probability beta uses.
    fresh_signals = data["signals"]
    fresh_book = data.get("book")
    fresh_signals["_beta_breakdown"] = fresh_breakdown

    if fresh_book is not None:
        price_info = fresh_signals.get("market_price") or {}
        market_no_price = 1.0 - float(price_info.get("yes_price", 0.5))
        fresh_true_prob = beta_blend_with_market(fresh_raw_prob, market_no_price)
    else:
        fresh_true_prob = fresh_raw_prob

    # ── Compute certainty for the original side ─────────────────────────────────
    if opp.side == "YES":
        fresh_certainty = fresh_true_prob
    else:
        fresh_certainty = 1.0 - fresh_true_prob

    entry_certainty = _extract_entry_certainty(opp)
    entry_forecast_high_f = _extract_entry_forecast_high(opp)
    fresh_forecast_high_f = fresh_breakdown.get("forecast_high_f")
    if fresh_forecast_high_f is not None:
        fresh_forecast_high_f = float(fresh_forecast_high_f)

    if entry_forecast_high_f is not None and fresh_forecast_high_f is not None:
        forecast_shift_f = fresh_forecast_high_f - entry_forecast_high_f
    else:
        forecast_shift_f = 0.0

    should_exit, trigger_reason = _should_trigger_exit(
        entry_certainty, fresh_certainty, forecast_shift_f
    )

    if not should_exit:
        logger.debug(
            f"[exit_monitor] opp {opp.id}: no exit "
            f"(entry={entry_certainty*100:.0f}% fresh={fresh_certainty*100:.0f}% "
            f"shift={forecast_shift_f:+.1f}°F)"
        )
        return None

    # ── Record the virtual exit ─────────────────────────────────────────────────
    theoretical_exit_price = await _get_theoretical_exit_price(opp, outcome)

    theoretical_pnl: Optional[float] = None
    if (
        theoretical_exit_price is not None
        and opp.virtual_entry_price is not None
        and opp.virtual_shares is not None
    ):
        theoretical_pnl = round(
            float(opp.virtual_shares)
            * (float(theoretical_exit_price) - float(opp.virtual_entry_price)),
            4,
        )

    exit_row = VirtualExit(
        opportunity_id=opp.id,
        triggered_at=datetime.now(timezone.utc),
        theoretical_exit_price=theoretical_exit_price,
        entry_confidence=int(entry_certainty * 100),
        exit_confidence=int(fresh_certainty * 100),
        forecast_shift_f=round(forecast_shift_f, 2),
        trigger_reason=trigger_reason,
        signals_at_exit=fresh_signals,
        theoretical_pnl=theoretical_pnl,
    )
    db.add(exit_row)
    await db.commit()
    await db.refresh(exit_row)

    logger.info(
        f"[exit_monitor] EXIT SIGNAL for opp {opp.id} "
        f"({city.name}, {opp.side}): {trigger_reason}"
    )
    return exit_row
