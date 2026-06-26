"""Virtual stop-loss / exit monitor for open virtual positions (alpha + beta).

Completely isolated from all existing virtual-position logic. It:
  1. Loads all Opportunity rows with virtual_status="open" (both estimators).
  2. Groups them by market and re-runs the matching estimator pipeline with
     FRESH forecasts — replicating each detector's exact math (collect all
     outcomes → normalise across the market → market-blend for beta).
  3. If a fresh estimate diverges materially from the entry estimate, records a
     VirtualExit row and sends a prominent Telegram alert.

Nothing in the original Opportunity row is ever modified.

Estimator handling:
  - "beta"  → beta_estimate_with_breakdown + normalise + beta_blend_with_market
  - "alpha" → estimate_with_breakdown + normalise (NULL estimator = alpha)
Both estimators store forecast_high_f in their breakdown, so the forecast-shift
trigger is estimator-independent; only the probability path differs.

Trigger conditions (see _should_trigger_exit):
  - Primary dual: certainty drops >= EXIT_CONFIDENCE_DROP_PP AND forecast shifts
    >= EXIT_FORECAST_SHIFT_F.
  - Floor breach: certainty collapses below EXIT_CERTAINTY_FLOOR (alone).
  - Extreme shift: forecast shifts >= EXIT_FORECAST_SHIFT_EXTREME_F (alone).
"""
import logging
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.analyzers.signal_aggregator import SignalAggregator
from app.analyzers.beta_estimator import (
    beta_estimate_with_breakdown,
    beta_blend_with_market,
    _clip as _beta_clip,
)
from app.analyzers.probability_estimator import (
    estimate_with_breakdown,
    _clip as _alpha_clip,
)
from app.analyzers.opportunity_detector import normalization_scale
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

_exit_aggregator = SignalAggregator()
_exit_poly_col = PolymarketCollector()


def _normalize_estimator(value: Optional[str]) -> str:
    """NULL/blank estimator on legacy rows is treated as 'alpha'."""
    return (value or "alpha").lower()


def _extract_entry_forecast_high(opp: Opportunity) -> Optional[float]:
    """Pull the forecast high stored when the position was opened.

    Beta stores its breakdown under signals['_beta_breakdown']; alpha stores it
    under signals['_blend']. Check both so the monitor is estimator-agnostic.
    """
    try:
        signals = opp.signals or {}
        for key in ("_beta_breakdown", "_blend"):
            breakdown = signals.get(key) or {}
            v = breakdown.get("forecast_high_f")
            if v is not None:
                return float(v)
        return None
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


# ── Fresh per-market re-estimation ──────────────────────────────────────────────

async def _load_city_skill_safe(db: AsyncSession, city_id: int, days_ahead: int) -> dict:
    """Load per-city skill for beta; never raises."""
    try:
        from app.analyzers.beta_opportunity_detector import _load_city_skill
        return await _load_city_skill(db, city_id, days_ahead)
    except Exception as e:
        logger.warning(f"[exit_monitor] _load_city_skill failed for city {city_id}: {e}")
        return {}


async def _fresh_market_estimates(
    db: AsyncSession,
    city: City,
    market: Market,
    outcomes: List[MarketOutcome],
    days_ahead: int,
    need_alpha: bool,
    need_beta: bool,
) -> Dict[int, dict]:
    """Re-estimate every outcome of a market with fresh forecasts.

    Returns {outcome_id: {"alpha_true": float|None, "alpha_fc": float|None,
                          "beta_true": float|None,  "beta_fc": float|None}}.

    Replicates each detector's exact pipeline so the fresh certainty is directly
    comparable to the stored confidence_score:
      alpha → estimate_with_breakdown → normalise across market
      beta  → beta_estimate_with_breakdown → normalise → beta_blend_with_market
    """
    city_lat = float(city.nws_lat) if city.nws_lat is not None else None
    city_lon = float(city.nws_lon) if city.nws_lon is not None else None

    city_skill: dict = {}
    if need_beta:
        city_skill = await _load_city_skill_safe(db, city.id, days_ahead)

    collected: List[dict] = []
    for outcome in outcomes:
        try:
            signals = await _exit_aggregator.aggregate(
                db=db,
                city_id=city.id,
                primary_icao=city.primary_icao,
                reference_icao=city.reference_icao,
                outcome=outcome,
                forecast_date=market.event_date,
                is_low_market=False,
                city_lat=city_lat,
                city_lon=city_lon,
                city_tz=city.timezone,
                onshore_wind_dir=getattr(city, "onshore_wind_dir", None),
            )
        except Exception as e:
            logger.warning(f"[exit_monitor] aggregate failed for outcome {outcome.id}: {e}")
            continue

        if not (signals.get("market_price") or {}):
            continue

        bucket_unit = resolve_bucket_unit(outcome)
        entry: dict = {
            "outcome": outcome,
            "signals": signals,
            "yes_price": (signals.get("market_price") or {}).get("yes_price"),
        }

        if need_alpha:
            try:
                a_raw, a_bd = estimate_with_breakdown(
                    signals, outcome.bucket_min, outcome.bucket_max,
                    days_ahead=days_ahead, bucket_unit=bucket_unit,
                )
                if a_bd.get("has_forecast_data"):
                    entry["alpha_raw"] = a_raw
                    entry["alpha_fc"] = a_bd.get("forecast_high_f")
            except Exception as e:
                logger.debug(f"[exit_monitor] alpha estimate failed for outcome {outcome.id}: {e}")

        if need_beta:
            try:
                b_raw, b_bd = beta_estimate_with_breakdown(
                    signals, outcome.bucket_min, outcome.bucket_max,
                    days_ahead=days_ahead, bucket_unit=bucket_unit,
                    city_skill=city_skill,
                )
                if b_bd.get("has_forecast_data"):
                    entry["beta_raw"] = b_raw
                    entry["beta_fc"] = b_bd.get("forecast_high_f")
            except Exception as e:
                logger.debug(f"[exit_monitor] beta estimate failed for outcome {outcome.id}: {e}")

        collected.append(entry)

    n_outcomes = len(outcomes)

    # Alpha normalisation (matches opportunity_detector phase 2).
    if need_alpha:
        alpha_raws = [e["alpha_raw"] for e in collected if "alpha_raw" in e]
        scale = normalization_scale(alpha_raws, n_outcomes)
        for e in collected:
            if "alpha_raw" not in e:
                continue
            e["alpha_true"] = (
                _alpha_clip(e["alpha_raw"] * scale) if scale is not None
                else _alpha_clip(e["alpha_raw"])
            )

    # Beta normalisation + market blend (matches beta_opportunity_detector).
    if need_beta:
        beta_raws = [e["beta_raw"] for e in collected if "beta_raw" in e]
        scale = normalization_scale(beta_raws, n_outcomes)
        for e in collected:
            if "beta_raw" not in e:
                continue
            nb = (
                _beta_clip(e["beta_raw"] * scale) if scale is not None
                else _beta_clip(e["beta_raw"])
            )
            blended, _info = beta_blend_with_market(nb, e.get("yes_price"))
            e["beta_true"] = _beta_clip(blended)

    return {
        e["outcome"].id: {
            "alpha_true": e.get("alpha_true"),
            "alpha_fc": e.get("alpha_fc"),
            "beta_true": e.get("beta_true"),
            "beta_fc": e.get("beta_fc"),
            "signals": e["signals"],
        }
        for e in collected
    }


async def check_open_positions(db: AsyncSession) -> List[VirtualExit]:
    """Scan all open virtual positions (alpha + beta) and record exits.

    Returns the list of VirtualExit rows just written to the DB.
    Never raises — all exceptions are caught internally.
    """
    exits_created: List[VirtualExit] = []

    try:
        result = await db.execute(
            select(Opportunity).where(Opportunity.virtual_status == "open")
        )
        open_opps: List[Opportunity] = result.scalars().all()
    except Exception as e:
        logger.error(f"[exit_monitor] Failed to load open positions: {e}", exc_info=True)
        return exits_created

    if not open_opps:
        return exits_created

    logger.info(f"[exit_monitor] Checking {len(open_opps)} open position(s).")

    # Group open positions by market so we collect fresh signals once per market.
    by_market: Dict[int, List[Opportunity]] = {}
    outcome_to_market: Dict[int, int] = {}
    for opp in open_opps:
        outcome_result = await db.execute(
            select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id)
        )
        outcome = outcome_result.scalar_one_or_none()
        if outcome is None:
            continue
        outcome_to_market[opp.outcome_id] = outcome.market_id
        by_market.setdefault(outcome.market_id, []).append(opp)

    for market_id, opps in by_market.items():
        try:
            rows = await _check_market(db, market_id, opps)
            exits_created.extend(rows)
        except Exception as e:
            logger.error(
                f"[exit_monitor] Error checking market {market_id}: {e}", exc_info=True
            )

    return exits_created


async def _check_market(
    db: AsyncSession,
    market_id: int,
    opps: List[Opportunity],
) -> List[VirtualExit]:
    """Re-evaluate every open position in one market."""
    created: List[VirtualExit] = []

    market_result = await db.execute(select(Market).where(Market.id == market_id))
    market: Optional[Market] = market_result.scalar_one_or_none()
    if market is None or market.resolved:
        return created
    if market.event_date < date.today():
        return created

    city_result = await db.execute(select(City).where(City.id == market.city_id))
    city: Optional[City] = city_result.scalar_one_or_none()
    if city is None:
        return created

    outcomes_result = await db.execute(
        select(MarketOutcome).where(MarketOutcome.market_id == market_id)
    )
    outcomes: List[MarketOutcome] = outcomes_result.scalars().all()
    if not outcomes:
        return created

    days_ahead = (market.event_date - date.today()).days

    estimators = {_normalize_estimator(o.estimator) for o in opps}
    need_alpha = "alpha" in estimators
    need_beta = "beta" in estimators

    estimates = await _fresh_market_estimates(
        db, city, market, outcomes, days_ahead, need_alpha, need_beta
    )

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(
        tzinfo=timezone.utc
    )

    for opp in opps:
        try:
            # One exit alert per position per calendar day.
            existing = await db.execute(
                select(VirtualExit.id).where(
                    VirtualExit.opportunity_id == opp.id,
                    VirtualExit.triggered_at >= today_start,
                ).limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                continue

            est = _normalize_estimator(opp.estimator)
            fresh = estimates.get(opp.outcome_id)
            if fresh is None:
                continue

            if est == "beta":
                fresh_true_prob = fresh.get("beta_true")
                fresh_fc = fresh.get("beta_fc")
            else:
                fresh_true_prob = fresh.get("alpha_true")
                fresh_fc = fresh.get("alpha_fc")

            if fresh_true_prob is None:
                continue

            if opp.side == "YES":
                fresh_certainty = fresh_true_prob
            else:
                fresh_certainty = 1.0 - fresh_true_prob

            entry_certainty = _extract_entry_certainty(opp)
            entry_fc = _extract_entry_forecast_high(opp)
            fresh_fc = float(fresh_fc) if fresh_fc is not None else None

            if entry_fc is not None and fresh_fc is not None:
                forecast_shift_f = fresh_fc - entry_fc
            else:
                forecast_shift_f = 0.0

            should_exit, reason = _should_trigger_exit(
                entry_certainty, fresh_certainty, forecast_shift_f
            )
            if not should_exit:
                logger.debug(
                    f"[exit_monitor] opp {opp.id} ({est}): no exit "
                    f"(entry={entry_certainty*100:.0f}% fresh={fresh_certainty*100:.0f}% "
                    f"shift={forecast_shift_f:+.1f}°F)"
                )
                continue

            row = await _record_exit(
                db, opp, fresh, entry_certainty, fresh_certainty,
                forecast_shift_f, reason,
            )
            if row is not None:
                created.append(row)
                logger.info(
                    f"[exit_monitor] EXIT SIGNAL for opp {opp.id} "
                    f"({est}, {city.name}, {opp.side}): {reason}"
                )
        except Exception as e:
            logger.error(f"[exit_monitor] Error on opp {opp.id}: {e}", exc_info=True)

    return created


async def _record_exit(
    db: AsyncSession,
    opp: Opportunity,
    fresh: dict,
    entry_certainty: float,
    fresh_certainty: float,
    forecast_shift_f: float,
    reason: str,
) -> Optional[VirtualExit]:
    """Write a VirtualExit row for a triggered position."""
    outcome_result = await db.execute(
        select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id)
    )
    outcome = outcome_result.scalar_one_or_none()
    if outcome is None:
        return None

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
        trigger_reason=reason,
        signals_at_exit=fresh.get("signals"),
        theoretical_pnl=theoretical_pnl,
    )
    db.add(exit_row)
    await db.commit()
    await db.refresh(exit_row)
    return exit_row
