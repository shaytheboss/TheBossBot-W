import logging
import secrets
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.metar import MetarObservation
from app.models.opportunity import Opportunity
from app.models.alert import Alert, TelegramUser
from app.utils.log_buffer import recent_logs
from app.utils.polymarket_discovery import (
    GAMMA_API, build_all_candidates, fetch_event_by_slug, fetch_events_by_tag,
)
from app.utils.units import resolve_bucket_unit, temp_in_bucket

logger = logging.getLogger(__name__)

router = APIRouter()

_ACTIVE_TOKENS: set[str] = set()
ADMIN_COOKIE_NAME = "admin_session"


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as db:
        yield db


def _check_admin(session: Optional[str]) -> None:
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="Admin not configured (ADMIN_PASSWORD missing)")
    if not session or session not in _ACTIVE_TOKENS:
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin(admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_COOKIE_NAME)):
    _check_admin(admin_session)
    return admin_session


class LoginIn(BaseModel):
    password: str


class SettingsIn(BaseModel):
    min_confidence_for_alert: Optional[int] = None
    min_edge_for_alert: Optional[float] = None
    max_days_ahead_for_alert: Optional[int] = None
    min_confidence_alert_near: Optional[float] = None
    min_confidence_alert_far: Optional[float] = None
    min_confidence_buy_near: Optional[float] = None
    min_confidence_buy_far: Optional[float] = None


class CityCreateIn(BaseModel):
    name: str
    primary_icao: str
    reference_icao: Optional[str] = None
    polymarket_slug: Optional[str] = None
    nws_lat: Optional[float] = None
    nws_lon: Optional[float] = None
    wunderground_url: Optional[str] = None
    timezone: str = "America/Los_Angeles"
    buoy_id: Optional[str] = None
    active: bool = True


@router.post("/login")
async def admin_login(body: LoginIn, response: Response):
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD not configured")
    if not secrets.compare_digest(body.password, settings.admin_password):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = secrets.token_urlsafe(32)
    _ACTIVE_TOKENS.add(token)
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )
    return {"ok": True}


@router.post("/logout")
async def admin_logout(
    response: Response,
    admin_session: Optional[str] = Cookie(default=None, alias=ADMIN_COOKIE_NAME),
):
    if admin_session:
        _ACTIVE_TOKENS.discard(admin_session)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def admin_me(_: str = Depends(require_admin)):
    return {"ok": True}


@router.post("/seed")
async def admin_seed(_: str = Depends(require_admin)):
    try:
        from app.utils.seed import seed_cities
        summary = await seed_cities()
        logger.info(f"Admin triggered seed: {summary}")
        return {"ok": True, **summary}
    except Exception as e:
        logger.error(f"Admin seed failed: {e}", exc_info=True)
        raise HTTPException(500, f"Seed failed: {e}")


@router.post("/discover")
async def admin_discover(_: str = Depends(require_admin)):
    from app.workers.jobs import job_discover_markets
    stats = await job_discover_markets(notify=False)
    return {"ok": True, **stats}


# Resolution uses the shared temp_in_bucket() helper (app/utils/units.py) so the
# admin force-resolve path and the scheduled job_check_resolutions path apply
# identical, float-dust-safe bucket math.


@router.post("/resolve-pending")
async def admin_resolve_pending(_: str = Depends(require_admin)):
    """Force-run resolution for past markets and settle stuck virtual positions.

    Two phases:
      1. Run job_check_resolutions() — handles unresolved markets the normal
         way (marks resolved + settles opportunities with alert_sent=True).
      2. Sweep stragglers: any virtual position still 'open' on a past market.
         These are typically opps where alert_sent=False (e.g. because the
         telegram bot token was missing when the opp was created, or
         send_opportunity_alert errored). The scheduled job skips them
         forever; this admin sweep settles them by checking the actual high.
    """
    from app.workers.jobs import job_check_resolutions

    today = date_cls.today()
    now = datetime.now(timezone.utc)

    # Phase 1: normal resolution job
    try:
        await job_check_resolutions()
    except Exception as e:
        logger.error(f"Admin resolve-pending (job phase) failed: {e}", exc_info=True)
        raise HTTPException(500, f"Resolve failed: {e}")

    # Phase 2: sweep stuck open virtual positions on past markets
    settled = 0
    skipped_no_metar = 0
    skipped_no_shares = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Opportunity, MarketOutcome, Market, City)
            .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
            .join(Market, Market.id == MarketOutcome.market_id)
            .join(City, City.id == Market.city_id)
            .where(
                Opportunity.virtual_status == "open",
                Market.event_date < today,
            )
        )).all()

        # Cache actual-high lookups per (icao, date) so we don't re-query
        # the same METAR window for each opportunity in the same market.
        highs_cache: dict = {}

        for opp, oc, market, city in rows:
            if not opp.virtual_shares:
                skipped_no_shares += 1
                continue

            key = (city.primary_icao, market.event_date)
            if key not in highs_cache:
                day_start = datetime(
                    market.event_date.year, market.event_date.month, market.event_date.day,
                    tzinfo=timezone.utc,
                )
                day_end = day_start + timedelta(days=1)
                actual_raw = (await db.execute(
                    select(func.max(MetarObservation.temperature_f)).where(
                        MetarObservation.icao == city.primary_icao,
                        MetarObservation.observed_at >= day_start,
                        MetarObservation.observed_at < day_end,
                    )
                )).scalar_one_or_none()
                highs_cache[key] = float(actual_raw) if actual_raw is not None else None

            actual_f = highs_cache[key]
            if actual_f is None:
                skipped_no_metar += 1
                continue
            actual_c = (actual_f - 32.0) * 5.0 / 9.0
            unit = resolve_bucket_unit(oc)
            actual_in_unit = actual_c if unit == "C" else actual_f

            won = temp_in_bucket(oc.bucket_min, oc.bucket_max, actual_in_unit)
            if opp.side == "YES":
                opp.outcome = "WIN" if won else "LOSS"
            else:
                opp.outcome = "WIN" if not won else "LOSS"
            opp.closed_at = now
            cost = float(opp.virtual_cost or 0.0)
            if opp.outcome == "WIN":
                opp.virtual_payout = float(opp.virtual_shares) * 1.00
                opp.virtual_status = "win"
            else:
                opp.virtual_payout = 0.0
                opp.virtual_status = "loss"
            opp.virtual_pnl = float(opp.virtual_payout) - cost
            settled += 1

            # If the market itself was still unresolved (shouldn't happen
            # often after phase 1, but possible if phase 1 had no METAR for
            # a different opp's city), mark it resolved now.
            if not market.resolved:
                market.resolved = True
                market.resolution_value = f"{actual_f}°F"

        if settled > 0:
            await db.commit()

    # Final counts for the UI
    async with AsyncSessionLocal() as db:
        markets_still_pending = (await db.execute(
            select(func.count(Market.id)).where(
                Market.resolved == False, Market.event_date < today
            )
        )).scalar() or 0
        positions_still_open = (await db.execute(
            select(func.count(Opportunity.id))
            .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
            .join(Market, Market.id == MarketOutcome.market_id)
            .where(
                Opportunity.virtual_status == "open",
                Market.event_date < today,
            )
        )).scalar() or 0

    logger.info(
        f"Admin resolve-pending: settled {settled} stuck positions, "
        f"skipped {skipped_no_metar} (no METAR), {skipped_no_shares} (no shares); "
        f"still pending: {markets_still_pending} markets, {positions_still_open} positions"
    )

    return {
        "ok": True,
        "positions_settled": settled,
        "positions_skipped_no_metar": skipped_no_metar,
        "markets_still_pending": int(markets_still_pending),
        "positions_still_open_past_date": int(positions_still_open),
    }


@router.post("/retroactive-resolution-fix")
async def admin_retroactive_resolution_fix(_: str = Depends(require_admin)):
    """Re-resolve already-settled markets using Polymarket's authoritative data.

    Queries Polymarket's Gamma API for each resolved market to find which bucket
    actually won, then corrects any opportunity outcomes (WIN/LOSS) that were
    recorded incorrectly based on METAR temperature rounding differences.

    Safe to run multiple times — only changes records that differ from Polymarket.
    """
    from app.workers.jobs import job_retroactive_resolution_fix
    try:
        summary = await job_retroactive_resolution_fix()
        return {"ok": True, **summary}
    except Exception as e:
        logger.error(f"Admin retroactive-resolution-fix failed: {e}", exc_info=True)
        raise HTTPException(500, f"Retroactive fix failed: {e}")


@router.get("/diag/polymarket")
async def admin_diag_polymarket(
    slug: str = Query(..., description="Exact Polymarket event slug to fetch"),
    _: str = Depends(require_admin),
):
    async with httpx.AsyncClient(headers={"User-Agent": "weather-arb-bot/1.0"}) as client:
        try:
            r = await client.get(
                f"{GAMMA_API}/events",
                params={"slug": slug, "limit": 1},
                timeout=20.0,
            )
        except Exception as e:
            raise HTTPException(502, f"Upstream error: {e}")
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:1000]}
        return {
            "status": r.status_code,
            "url": str(r.url),
            "body": body,
        }


@router.get("/diag/last-discovery")
async def admin_diag_last_discovery(_: str = Depends(require_admin)):
    from app.workers.jobs import LAST_DISCOVERY
    return LAST_DISCOVERY


@router.get("/diag/candidates")
async def admin_diag_candidates(
    _: str = Depends(require_admin), db: AsyncSession = Depends(get_db),
    days: int = Query(default=7, ge=1, le=14),
):
    cities = (await db.execute(
        select(City).where(City.active == True, City.polymarket_slug != None)
    )).scalars().all()
    candidates = build_all_candidates([c.polymarket_slug for c in cities], days)
    return {
        "city_count": len(cities),
        "days": days + 1,
        "total": len(candidates),
        "sample": [s for _c, s, _d in candidates[:30]],
    }


def _parse_iso_date(s: Optional[str], name: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = date_cls.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"Invalid {name}: {s!r}. Use YYYY-MM-DD.")
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


@router.get("/stats")
async def admin_stats(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    from_date: Optional[str] = Query(default=None, description="ISO YYYY-MM-DD; filter by detected_at"),
    to_date: Optional[str] = Query(default=None, description="ISO YYYY-MM-DD; exclusive upper"),
    city_id: Optional[int] = Query(default=None),
):
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None

    def opp_q(base):
        q = base
        if from_dt is not None:
            q = q.where(Opportunity.detected_at >= from_dt)
        if to_dt is not None:
            q = q.where(Opportunity.detected_at < to_dt)
        if city_id is not None:
            q = q.join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id) \
                 .join(Market, Market.id == MarketOutcome.market_id) \
                 .where(Market.city_id == city_id)
        return q

    total_opps = (await db.execute(opp_q(select(func.count(Opportunity.id))))).scalar() or 0
    alerted = (await db.execute(
        opp_q(select(func.count(Opportunity.id))).where(Opportunity.alert_sent == True)
    )).scalar() or 0
    wins = (await db.execute(
        opp_q(select(func.count(Opportunity.id))).where(Opportunity.outcome == "WIN")
    )).scalar() or 0
    losses = (await db.execute(
        opp_q(select(func.count(Opportunity.id))).where(Opportunity.outcome == "LOSS")
    )).scalar() or 0
    open_pos = (await db.execute(
        opp_q(select(func.count(Opportunity.id)))
        .where(Opportunity.alert_sent == True, Opportunity.outcome == None)
    )).scalar() or 0

    cities = (await db.execute(select(func.count(City.id)))).scalar() or 0
    markets = (await db.execute(select(func.count(Market.id)))).scalar() or 0
    outcomes = (await db.execute(select(func.count(MarketOutcome.id)))).scalar() or 0
    outcomes_with_token = (
        await db.execute(
            select(func.count(MarketOutcome.id)).where(MarketOutcome.token_id != None)
        )
    ).scalar() or 0
    telegram_users = (await db.execute(select(func.count(TelegramUser.id)))).scalar() or 0

    win_rate = round(wins / max(wins + losses, 1) * 100, 1)

    positions_opened = (await db.execute(
        opp_q(select(func.count(Opportunity.id)))
        .where(Opportunity.virtual_shares != None)
    )).scalar() or 0
    # Cost of CLOSED positions only (win + loss) — used together with total_payout
    # and net_pnl so the three numbers are internally consistent:
    #   net_pnl = total_payout - total_cost  (within closed positions)
    total_cost = (await db.execute(
        opp_q(select(func.coalesce(func.sum(Opportunity.virtual_cost), 0.0)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar() or 0.0
    # Cost of still-open positions = money at risk, not yet resolved
    open_cost = (await db.execute(
        opp_q(select(func.coalesce(func.sum(Opportunity.virtual_cost), 0.0)))
        .where(Opportunity.virtual_status == "open")
    )).scalar() or 0.0
    total_payout = (await db.execute(
        opp_q(select(func.coalesce(func.sum(Opportunity.virtual_payout), 0.0)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar() or 0.0
    net_pnl = (await db.execute(
        opp_q(select(func.coalesce(func.sum(Opportunity.virtual_pnl), 0.0)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar() or 0.0
    pos_wins = (await db.execute(
        opp_q(select(func.count(Opportunity.id)))
        .where(Opportunity.virtual_status == "win")
    )).scalar() or 0
    pos_losses = (await db.execute(
        opp_q(select(func.count(Opportunity.id)))
        .where(Opportunity.virtual_status == "loss")
    )).scalar() or 0
    pos_win_rate = round(pos_wins / max(pos_wins + pos_losses, 1) * 100, 1)
    # avg_cost across all opened positions (closed + open), so use total_cost + open_cost
    avg_cost = (float(total_cost + open_cost) / positions_opened) if positions_opened else 0.0
    best_pnl = (await db.execute(
        opp_q(select(func.max(Opportunity.virtual_pnl)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar()
    worst_pnl = (await db.execute(
        opp_q(select(func.min(Opportunity.virtual_pnl)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar()

    return {
        "filter": {
            "from_date": from_date,
            "to_date": to_date,
            "city_id": city_id,
        },
        "opportunities": {
            "total": total_opps,
            "alerted": alerted,
            "open": open_pos,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
        },
        "positions": {
            "opened": positions_opened,
            "total_cost": round(float(total_cost), 2),        # closed positions only
            "open_cost": round(float(open_cost), 2),          # still-open (at risk)
            "total_payout": round(float(total_payout), 2),
            "net_pnl": round(float(net_pnl), 2),
            "wins": pos_wins,
            "losses": pos_losses,
            "win_rate_pct": pos_win_rate,
            "avg_cost": round(avg_cost, 2),
            "best_pnl": round(float(best_pnl), 2) if best_pnl is not None else None,
            "worst_pnl": round(float(worst_pnl), 2) if worst_pnl is not None else None,
        },
        "inventory": {
            "cities": cities,
            "markets": markets,
            "outcomes": outcomes,
            "outcomes_with_token": outcomes_with_token,
            "telegram_users": telegram_users,
        },
    }


@router.get("/positions")
async def admin_positions(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    city_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, le=1000),
):
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None

    q = (
        select(Opportunity, MarketOutcome, Market, City)
        .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
        .join(Market, Market.id == MarketOutcome.market_id)
        .join(City, City.id == Market.city_id)
        .where(Opportunity.virtual_shares != None)
        .order_by(desc(Opportunity.detected_at))
        .limit(limit)
    )
    if from_dt is not None:
        q = q.where(Opportunity.detected_at >= from_dt)
    if to_dt is not None:
        q = q.where(Opportunity.detected_at < to_dt)
    if city_id is not None:
        q = q.where(Market.city_id == city_id)

    rows = (await db.execute(q)).all()
    out = []
    for opp, oc, market, city in rows:
        out.append({
            "id": opp.id,
            "detected_at": opp.detected_at.isoformat() if opp.detected_at else None,
            "event_date": market.event_date.isoformat() if market.event_date else None,
            "city": city.name,
            "market": market.question,
            "bucket": oc.bucket_label,
            "side": opp.side,
            "shares": opp.virtual_shares,
            "entry_price": float(opp.virtual_entry_price) if opp.virtual_entry_price is not None else None,
            "cost": float(opp.virtual_cost) if opp.virtual_cost is not None else None,
            "payout": float(opp.virtual_payout) if opp.virtual_payout is not None else None,
            "pnl": float(opp.virtual_pnl) if opp.virtual_pnl is not None else None,
            "status": opp.virtual_status,
            "outcome": opp.outcome,
            # Mirror the data shown on the Opportunities screen so a position
            # row carries its own probability estimate / edge / link, and the
            # alert text stays reachable via /opportunities/{id}/alert using
            # this same opp id (the existing "View" modal endpoint).
            "true_prob": float(opp.estimated_true_prob) if opp.estimated_true_prob is not None else None,
            "market_price": float(opp.market_price) if opp.market_price is not None else None,
            "edge": float(opp.edge) if opp.edge is not None else None,
            "confidence": opp.confidence_score,
            "market_url": (
                f"https://polymarket.com/event/{market.external_id}"
                if market.external_id else None
            ),
        })
    return out


def _stat_block(items: list[dict]) -> dict:
    """Summarise a group of settled bets: win-rate, Brier, calibration gap, P&L.

    Each item is {pred, won, pnl, entry}. `pred` is the model probability for
    the side actually taken (0-1), `won` is 1/0, `pnl` is virtual P&L (may be
    None), `entry` is the per-share entry price paid (0-1, the break-even
    win-rate for that bet).
    """
    n = len(items)
    if n == 0:
        return {
            "n": 0, "wins": 0, "win_rate": None, "avg_pred": None,
            "brier": None, "calibration_gap": None, "net_pnl": 0.0,
            "avg_entry": None, "breakeven_win_rate": None, "edge_real": None,
        }
    wins = sum(it["won"] for it in items)
    win_rate = wins / n
    avg_pred = sum(it["pred"] for it in items) / n
    brier = sum((it["pred"] - it["won"]) ** 2 for it in items) / n
    pnls = [it["pnl"] for it in items if it["pnl"] is not None]
    net_pnl = sum(pnls) if pnls else 0.0
    entries = [it["entry"] for it in items if it["entry"] is not None]
    avg_entry = (sum(entries) / len(entries)) if entries else None
    return {
        "n": n,
        "wins": wins,
        "win_rate": round(win_rate * 100, 1),
        "avg_pred": round(avg_pred * 100, 1),
        # How far the model's stated confidence is above its real hit-rate.
        # Positive = overconfident.
        "calibration_gap": round((avg_pred - win_rate) * 100, 1),
        "brier": round(brier, 4),
        "net_pnl": round(net_pnl, 2),
        "avg_entry": round(avg_entry * 100, 1) if avg_entry is not None else None,
        # Break-even win-rate for these bets == average entry price.
        "breakeven_win_rate": round(avg_entry * 100, 1) if avg_entry is not None else None,
        # Real edge = actual win-rate minus what you had to pay (break-even).
        # Negative = losing money on average even if win-rate looks high.
        "edge_real": (
            round((win_rate - avg_entry) * 100, 1) if avg_entry is not None else None
        ),
    }


@router.get("/lessons")
async def admin_lessons(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    city_id: Optional[int] = Query(default=None),
):
    """Calibration / post-mortem report over settled opportunities.

    Turns the accumulated WIN/LOSS history into actionable lessons: how
    overconfident the model is, whether bets actually make money after the
    break-even price, and where the errors concentrate (lead time, city,
    side, entry-price band, open-ended buckets).
    """
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None

    q = (
        select(Opportunity, MarketOutcome, Market, City)
        .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
        .join(Market, Market.id == MarketOutcome.market_id)
        .join(City, City.id == Market.city_id)
        .where(Opportunity.outcome.in_(["WIN", "LOSS"]))
        .order_by(desc(Opportunity.detected_at))
    )
    if from_dt is not None:
        q = q.where(Opportunity.detected_at >= from_dt)
    if to_dt is not None:
        q = q.where(Opportunity.detected_at < to_dt)
    if city_id is not None:
        q = q.where(Market.city_id == city_id)

    rows = (await db.execute(q)).all()

    items: list[dict] = []
    by_lead: dict = {}
    by_city: dict = {}
    by_side: dict = {}
    by_price_band: dict = {}
    by_conf_band: dict = {}
    open_ended: list[dict] = []
    closed_bucket: list[dict] = []

    def _band_label(p: float) -> str:
        # 10pp confidence bands from 50% upward.
        lo = int(p * 10) * 10
        return f"{lo}-{lo + 10}%"

    def _price_band_label(e: Optional[float]) -> str:
        if e is None:
            return "unknown"
        if e < 0.50:
            return "<50¢"
        if e < 0.65:
            return "50-64¢"
        if e < 0.75:
            return "65-74¢"
        if e < 0.85:
            return "75-84¢"
        return "85¢+"

    for opp, oc, market, city in rows:
        true_prob = float(opp.estimated_true_prob)
        pred = (1.0 - true_prob) if opp.side == "NO" else true_prob
        won = 1 if opp.outcome == "WIN" else 0
        pnl = float(opp.virtual_pnl) if opp.virtual_pnl is not None else None
        entry = float(opp.virtual_entry_price) if opp.virtual_entry_price is not None else None

        lead = None
        if market.event_date and opp.detected_at:
            lead = (market.event_date - opp.detected_at.date()).days

        item = {"pred": pred, "won": won, "pnl": pnl, "entry": entry}
        items.append(item)

        lead_key = "same-day" if lead == 0 else (f"{lead}-day" if lead is not None else "unknown")
        by_lead.setdefault(lead_key, []).append(item)
        by_city.setdefault(city.name, []).append(item)
        by_side.setdefault(opp.side, []).append(item)
        by_price_band.setdefault(_price_band_label(entry), []).append(item)
        by_conf_band.setdefault(_band_label(pred), []).append(item)

        if oc.bucket_min is None or oc.bucket_max is None:
            open_ended.append(item)
        else:
            closed_bucket.append(item)

    def _map(d: dict) -> dict:
        return {k: _stat_block(v) for k, v in d.items()}

    return {
        "filter": {"from_date": from_date, "to_date": to_date, "city_id": city_id},
        "overall": _stat_block(items),
        "by_confidence_band": _map(by_conf_band),
        "by_lead_time": _map(by_lead),
        "by_city": _map(by_city),
        "by_side": _map(by_side),
        "by_entry_price": _map(by_price_band),
        "bucket_shape": {
            "open_ended": _stat_block(open_ended),   # "X or higher" / "X or lower"
            "closed_range": _stat_block(closed_bucket),
        },
    }


@router.get("/opportunities")
async def admin_opportunities(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, le=500),
    only_alerted: bool = Query(default=False),
    outcome: Optional[str] = Query(default=None),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    city_id: Optional[int] = Query(default=None),
):
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None

    # Join through to Market only when a city filter is requested, so the
    # default (unfiltered) query stays as cheap as before.
    if city_id is not None:
        q = (
            select(Opportunity)
            .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
            .join(Market, Market.id == MarketOutcome.market_id)
            .where(Market.city_id == city_id)
            .order_by(desc(Opportunity.detected_at))
            .limit(limit)
        )
    else:
        q = select(Opportunity).order_by(desc(Opportunity.detected_at)).limit(limit)
    if only_alerted:
        q = q.where(Opportunity.alert_sent == True)
    if outcome:
        q = q.where(Opportunity.outcome == outcome.upper())
    if from_dt is not None:
        q = q.where(Opportunity.detected_at >= from_dt)
    if to_dt is not None:
        q = q.where(Opportunity.detected_at < to_dt)
    rows = (await db.execute(q)).scalars().all()

    out = []
    for opp in rows:
        oc_res = await db.execute(
            select(MarketOutcome).where(MarketOutcome.id == opp.outcome_id)
        )
        oc = oc_res.scalar_one_or_none()
        market = None
        city_name = None
        market_url = None
        if oc:
            mr = await db.execute(select(Market).where(Market.id == oc.market_id))
            market = mr.scalar_one_or_none()
            if market:
                cr = await db.execute(select(City).where(City.id == market.city_id))
                c = cr.scalar_one_or_none()
                city_name = c.name if c else None
                market_url = f"https://polymarket.com/event/{market.external_id}"
        out.append({
            "id": opp.id,
            "detected_at": opp.detected_at.isoformat() if opp.detected_at else None,
            "city": city_name,
            "market": market.question if market else None,
            "market_url": market_url,
            "event_date": market.event_date.isoformat() if market else None,
            "bucket": oc.bucket_label if oc else None,
            "side": opp.side,
            "market_price": float(opp.market_price),
            "true_prob": float(opp.estimated_true_prob),
            "edge": float(opp.edge),
            "confidence": opp.confidence_score,
            "alert_sent": opp.alert_sent,
            "outcome": opp.outcome,
            "closed_at": opp.closed_at.isoformat() if opp.closed_at else None,
        })
    return out


@router.get("/opportunities/{opp_id}/alert")
async def admin_opportunity_alert(
    opp_id: int,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return the full Telegram alert text for an opportunity.

    The text is the same message that was sent to users at detection time,
    so this is effectively the audit trail for why the opportunity was
    flagged.
    """
    opp = (await db.execute(
        select(Opportunity).where(Opportunity.id == opp_id)
    )).scalar_one_or_none()
    if not opp:
        raise HTTPException(404, "Opportunity not found")

    alert = (await db.execute(
        select(Alert)
        .where(Alert.opportunity_id == opp_id)
        .order_by(desc(Alert.sent_at))
        .limit(1)
    )).scalar_one_or_none()

    return {
        "opportunity_id": opp_id,
        "message_text": alert.message_text if alert else None,
        "sent_at": alert.sent_at.isoformat() if alert and alert.sent_at else None,
        "alert_sent": opp.alert_sent,
    }


@router.get("/cities")
async def admin_cities(_: str = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(City).order_by(City.name))).scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "primary_icao": c.primary_icao,
            "reference_icao": c.reference_icao,
            "polymarket_slug": c.polymarket_slug,
            "wunderground_url": c.wunderground_url,
            "timezone": c.timezone,
            "buoy_id": c.buoy_id,
            "nws_lat": float(c.nws_lat) if c.nws_lat else None,
            "nws_lon": float(c.nws_lon) if c.nws_lon else None,
            "active": c.active,
            "blacklisted": bool(getattr(c, "blacklisted", False)),
        }
        for c in rows
    ]


@router.post("/cities", status_code=201)
async def admin_city_create(
    body: CityCreateIn,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    payload = body.model_dump()
    payload["primary_icao"] = payload["primary_icao"].strip().upper()
    if payload.get("reference_icao"):
        payload["reference_icao"] = payload["reference_icao"].strip().upper()

    if len(payload["primary_icao"]) > 4:
        raise HTTPException(
            400,
            f"primary_icao must be a 4-character ICAO station code (e.g. KBOS, EGLL). "
            f"Got '{payload['primary_icao']}' ({len(payload['primary_icao'])} chars)."
        )
    if payload.get("reference_icao") and len(payload["reference_icao"]) > 4:
        raise HTTPException(
            400,
            f"reference_icao must be a 4-character ICAO station code. "
            f"Got '{payload['reference_icao']}' ({len(payload['reference_icao'])} chars)."
        )

    if not payload.get("wunderground_url"):
        payload["wunderground_url"] = ""

    city = City(**payload)
    db.add(city)
    await db.commit()
    await db.refresh(city)
    logger.info(f"Admin created city #{city.id} {city.name} ({city.primary_icao})")
    return {"ok": True, "id": city.id, "name": city.name}


@router.patch("/cities/{city_id}")
async def admin_city_update(
    city_id: int,
    payload: dict,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(City).where(City.id == city_id))
    city = res.scalar_one_or_none()
    if not city:
        raise HTTPException(404, "City not found")
    str_fields = (
        "name", "primary_icao", "reference_icao", "polymarket_slug",
        "wunderground_url", "timezone", "buoy_id",
    )
    for k in str_fields:
        if k in payload:
            val = payload[k]
            if k in ("primary_icao", "reference_icao") and val:
                val = str(val).strip().upper()
            setattr(city, k, val)
    if "active" in payload:
        city.active = bool(payload["active"])
    if "blacklisted" in payload:
        city.blacklisted = bool(payload["blacklisted"])
    for k in ("nws_lat", "nws_lon"):
        if k in payload and payload[k] is not None:
            setattr(city, k, float(payload[k]))
    await db.commit()
    return {"ok": True}


@router.delete("/cities/{city_id}", status_code=204)
async def admin_city_delete(
    city_id: int,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(City).where(City.id == city_id))
    city = res.scalar_one_or_none()
    if not city:
        raise HTTPException(404, "City not found")
    city_name = city.name
    await db.delete(city)
    await db.commit()
    logger.info(f"Admin deleted city #{city_id} {city_name}")
    return Response(status_code=204)


@router.get("/settings")
async def admin_get_settings(_: str = Depends(require_admin)):
    return {
        "min_confidence_for_alert": settings.min_confidence_for_alert,
        "min_edge_for_alert": settings.min_edge_for_alert,
        "max_days_ahead_for_alert": settings.max_days_ahead_for_alert,
        "min_confidence_alert_near": settings.min_confidence_alert_near,
        "min_confidence_alert_far": settings.min_confidence_alert_far,
        "min_confidence_buy_near": settings.min_confidence_buy_near,
        "min_confidence_buy_far": settings.min_confidence_buy_far,
        "metar_fetch_interval": settings.metar_fetch_interval,
        "polymarket_fetch_interval": settings.polymarket_fetch_interval,
        "analyzer_run_interval": settings.analyzer_run_interval,
        "alert_dedup_minutes": settings.alert_dedup_minutes,
        "app_env": settings.app_env,
    }


def _validate_unit(value: float, name: str) -> None:
    if not (0.0 <= value <= 1.0):
        raise HTTPException(400, f"{name} must be in [0.0, 1.0]")


@router.patch("/settings")
async def admin_set_settings(payload: SettingsIn, _: str = Depends(require_admin)):
    if payload.min_confidence_for_alert is not None:
        if not (0 <= payload.min_confidence_for_alert <= 100):
            raise HTTPException(400, "min_confidence_for_alert must be 0-100")
        settings.min_confidence_for_alert = payload.min_confidence_for_alert
    if payload.min_edge_for_alert is not None:
        if not (0.0 <= payload.min_edge_for_alert <= 1.0):
            raise HTTPException(400, "min_edge_for_alert must be 0.0-1.0")
        settings.min_edge_for_alert = payload.min_edge_for_alert
    if payload.max_days_ahead_for_alert is not None:
        if not (0 <= payload.max_days_ahead_for_alert <= 14):
            raise HTTPException(400, "max_days_ahead_for_alert must be 0-14")
        settings.max_days_ahead_for_alert = payload.max_days_ahead_for_alert
    if payload.min_confidence_alert_near is not None:
        _validate_unit(payload.min_confidence_alert_near, "min_confidence_alert_near")
        settings.min_confidence_alert_near = payload.min_confidence_alert_near
    if payload.min_confidence_alert_far is not None:
        _validate_unit(payload.min_confidence_alert_far, "min_confidence_alert_far")
        settings.min_confidence_alert_far = payload.min_confidence_alert_far
    if payload.min_confidence_buy_near is not None:
        _validate_unit(payload.min_confidence_buy_near, "min_confidence_buy_near")
        settings.min_confidence_buy_near = payload.min_confidence_buy_near
    if payload.min_confidence_buy_far is not None:
        _validate_unit(payload.min_confidence_buy_far, "min_confidence_buy_far")
        settings.min_confidence_buy_far = payload.min_confidence_buy_far
    if settings.min_confidence_buy_near < settings.min_confidence_alert_near:
        logger.warning(
            f"min_confidence_buy_near ({settings.min_confidence_buy_near}) < "
            f"min_confidence_alert_near ({settings.min_confidence_alert_near}); "
            "buy will be clamped to alert at runtime."
        )
    if settings.min_confidence_buy_far < settings.min_confidence_alert_far:
        logger.warning(
            f"min_confidence_buy_far ({settings.min_confidence_buy_far}) < "
            f"min_confidence_alert_far ({settings.min_confidence_alert_far}); "
            "buy will be clamped to alert at runtime."
        )
    logger.info(
        f"Admin updated thresholds: min_conf={settings.min_confidence_for_alert} "
        f"min_edge={settings.min_edge_for_alert} "
        f"max_days_ahead={settings.max_days_ahead_for_alert} "
        f"alert(near/far)={settings.min_confidence_alert_near}/{settings.min_confidence_alert_far} "
        f"buy(near/far)={settings.min_confidence_buy_near}/{settings.min_confidence_buy_far}"
    )
    return {"ok": True}


@router.get("/logs")
async def admin_logs(
    _: str = Depends(require_admin),
    limit: int = Query(default=200, le=500),
    level: Optional[str] = Query(default=None),
):
    return {"logs": recent_logs(limit=limit, level=level)}


class DataPruneRequest(BaseModel):
    before_date: str
    tables: List[str]


@router.delete("/data/prune")
async def admin_prune_data(
    req: DataPruneRequest,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        cutoff = datetime.fromisoformat(req.before_date).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, f"Invalid date format: {req.before_date!r}. Use YYYY-MM-DD.")

    want_opps = "opportunities" in req.tables or "all" in req.tables
    want_alerts = "alerts" in req.tables or "all" in req.tables
    deleted: dict = {}

    if want_alerts:
        result = await db.execute(
            delete(Alert).where(Alert.sent_at < cutoff)
        )
        deleted["alerts"] = result.rowcount

    if want_opps:
        result = await db.execute(
            delete(Opportunity).where(Opportunity.detected_at < cutoff)
        )
        deleted["opportunities"] = result.rowcount

    await db.commit()
    logger.info(f"Admin pruned data before {req.before_date}: {deleted}")
    return {"deleted": deleted, "before": req.before_date}
