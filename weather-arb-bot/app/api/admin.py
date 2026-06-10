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
    onshore_wind_dir: Optional[int] = None
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

    # Phase 2: sweep stuck open virtual positions on past markets.
    #
    # IMPORTANT: settle WIN/LOSS from Polymarket's official result ONLY — never
    # from METAR / temperature math. METAR can disagree with how Polymarket
    # actually settled the market (different station, rounding, observation
    # window), which previously produced wrong WIN/LOSS records. If Polymarket
    # has not settled a market yet, its stuck positions are left open and retried
    # on the next run.
    from app.workers.jobs import (
        _fetch_polymarket_winning_outcomes,
        _mark_outcome_winners,
    )

    settled = 0
    markets_resolved = 0
    skipped_no_poly = 0
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

        # Group stuck-open opps by market so we query the Polymarket API once
        # per market, then settle every position on it.
        by_market: dict[int, dict] = {}
        for opp, oc, market, city in rows:
            entry = by_market.setdefault(
                market.id, {"market": market, "opps": []}
            )
            entry["opps"].append(opp)

        dirty = False
        for market_id, entry in by_market.items():
            market = entry["market"]
            outcomes = (await db.execute(
                select(MarketOutcome).where(MarketOutcome.market_id == market.id)
            )).scalars().all()

            poly_winning, poly_resolved, poly_note = (
                await _fetch_polymarket_winning_outcomes(market.external_id, outcomes)
            )
            if not poly_resolved:
                skipped_no_poly += len(entry["opps"])
                continue

            # Persist the winning bucket(s) for model-accuracy scoring.
            _mark_outcome_winners(outcomes, set(poly_winning))
            dirty = True

            for opp in entry["opps"]:
                if not opp.virtual_shares:
                    skipped_no_shares += 1
                    continue
                won = opp.outcome_id in poly_winning
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
                dirty = True

            # Mark the market resolved from Polymarket if phase 1 didn't already.
            if not market.resolved:
                win_labels = [o.bucket_label for o in outcomes if o.id in poly_winning]
                market.resolved = True
                market.resolution_value = (
                    f"{', '.join(win_labels)} (Polymarket)" if win_labels
                    else f"Polymarket: {poly_note}"
                )
                markets_resolved += 1
                dirty = True

        if dirty:
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
        f"Admin resolve-pending: resolved {markets_resolved} markets, settled "
        f"{settled} stuck positions, skipped {skipped_no_poly} (not on Polymarket "
        f"yet), {skipped_no_shares} (no shares); still pending: "
        f"{markets_still_pending} markets, {positions_still_open} positions"
    )

    return {
        "ok": True,
        "markets_resolved": markets_resolved,
        "positions_settled": settled,
        "positions_skipped_not_on_poly": skipped_no_poly,
        "markets_still_pending": int(markets_still_pending),
        "positions_still_open_past_date": int(positions_still_open),
    }


@router.post("/retroactive-resolution-fix")
async def admin_retroactive_resolution_fix(_: str = Depends(require_admin)):
    """Start the retroactive resolution fix as a background task.

    Returns immediately with status="started". Poll
    GET /retroactive-resolution-fix/status for live progress.
    """
    import asyncio
    from app.workers.jobs import LAST_RETRO_FIX, job_retroactive_resolution_fix

    if LAST_RETRO_FIX.get("status") == "running":
        return {"ok": False, "already_running": True, **LAST_RETRO_FIX}

    asyncio.create_task(job_retroactive_resolution_fix())
    return {"ok": True, "started": True}


@router.get("/retroactive-resolution-fix/status")
async def admin_retro_fix_status(_: str = Depends(require_admin)):
    """Poll the current state of the retroactive resolution fix."""
    from app.workers.jobs import LAST_RETRO_FIX
    return LAST_RETRO_FIX


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


@router.get("/diag/resolution")
async def admin_diag_resolution(
    slug: str = Query(..., description="Polymarket event slug (= market external_id)"),
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Show exactly how a market would resolve from Polymarket's own data.

    For every sub-market (bucket) it prints the raw `outcomes`, `outcomePrices`,
    `resolved`/`closed` flags, and which side we conclude won — plus how that
    maps to our stored MarketOutcome records. Use this to verify the bot's
    reading matches the Polymarket UI, byte-for-byte, before trusting WIN/LOSS.
    """
    import json as _json

    async with httpx.AsyncClient(headers={"User-Agent": "weather-arb-bot/1.0"}) as client:
        try:
            r = await client.get(
                f"{GAMMA_API}/events",
                params={"slug": slug, "limit": 1},
                timeout=20.0,
            )
        except Exception as e:
            raise HTTPException(502, f"Upstream error: {e}")
        if r.status_code != 200:
            raise HTTPException(502, f"Gamma returned HTTP {r.status_code}")
        data = r.json()

    event = (
        data[0] if isinstance(data, list) and data
        else (data if isinstance(data, dict) and data.get("slug") else None)
    )
    if not event:
        return {"slug": slug, "found": False, "buckets": []}

    def _decode(raw):
        if isinstance(raw, str):
            try:
                v = _json.loads(raw)
                return v if isinstance(v, list) else []
            except (ValueError, TypeError):
                return []
        return list(raw) if isinstance(raw, (list, tuple)) else []

    # Our stored outcomes for this market (for the mapping column).
    market = (await db.execute(
        select(Market).where(Market.external_id == slug)
    )).scalar_one_or_none()
    db_outcomes = []
    if market:
        db_outcomes = (await db.execute(
            select(MarketOutcome).where(MarketOutcome.market_id == market.id)
        )).scalars().all()
    token_to_label = {o.token_id: o.bucket_label for o in db_outcomes if o.token_id}
    label_set = {o.bucket_label.strip().lower() for o in db_outcomes}

    buckets = []
    for m in event.get("markets") or []:
        outcomes_arr = _decode(m.get("outcomes"))
        prices_arr = _decode(m.get("outcomePrices"))
        tokens_arr = _decode(m.get("clobTokenIds"))
        yes_idx = next(
            (i for i, l in enumerate(outcomes_arr) if str(l).strip().lower() == "yes"),
            0,
        )
        yes_price = None
        if yes_idx < len(prices_arr):
            try:
                yes_price = float(prices_arr[yes_idx])
            except (ValueError, TypeError):
                yes_price = None
        yes_token = str(tokens_arr[yes_idx]) if yes_idx < len(tokens_arr) else None
        label = (m.get("groupItemTitle") or m.get("question") or "?").strip()
        buckets.append({
            "bucket": label,
            "resolved": m.get("resolved"),
            "closed": m.get("closed"),
            "outcomes": outcomes_arr,
            "outcomePrices": prices_arr,
            "yes_index": yes_idx,
            "yes_price": yes_price,
            "yes_won": (yes_price is not None and yes_price >= 0.99),
            "yes_token": yes_token,
            "matches_db_token": (yes_token in token_to_label) if yes_token else False,
            "matches_db_label": label.lower() in label_set,
        })

    # Run the real resolver so the diagnostic matches production exactly.
    from app.workers.jobs import _fetch_polymarket_winning_outcomes
    winning_ids, poly_resolved, note = (
        await _fetch_polymarket_winning_outcomes(slug, db_outcomes)
        if db_outcomes else (frozenset(), False, "no DB outcomes")
    )
    winners = [
        {"outcome_id": o.id, "bucket": o.bucket_label, "side_that_wins": "YES"}
        for o in db_outcomes if o.id in winning_ids
    ]

    return {
        "slug": slug,
        "found": True,
        "title": event.get("title"),
        "poly_resolved": poly_resolved,
        "resolver_note": note,
        "winning_outcome_ids": list(winning_ids),
        "winners": winners,
        "buckets": buckets,
        "db_outcome_count": len(db_outcomes),
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


# ── Per-model forecast helpers ──────────────────────────────────────────────
# Each opportunity persists a snapshot of every weather model's forecast in its
# `signals` JSONB (written by SignalAggregator). We surface those point
# forecasts so the dashboard can show what each model predicted and, once the
# market settles on Polymarket, whether that prediction was correct.

# (signals key, display label) — order controls display order.
_MODEL_SIGNAL_LABELS: list[tuple[str, str]] = [
    ("gfs_forecast", "GFS"),
    ("ecmwf_forecast", "ECMWF"),
    ("hrrr_forecast", "HRRR"),
    ("nws_forecast", "NWS"),
    ("tomorrowio_forecast", "Tomorrow.io"),
    ("meteosource_forecast", "Meteosource"),
    ("icon_forecast", "ICON"),
    ("wunderground_forecast", "Wunderground"),
]


def _extract_model_forecasts(signals) -> dict:
    """Return {model label → predicted daily high °F} from an opp's signals."""
    out: dict = {}
    if not isinstance(signals, dict):
        return out
    for key, label in _MODEL_SIGNAL_LABELS:
        fc = signals.get(key)
        if isinstance(fc, dict):
            v = fc.get("predicted_high_f")
            if v is not None:
                try:
                    out[label] = round(float(v), 1)
                except (TypeError, ValueError):
                    pass
    ens = signals.get("gfs_ensemble")
    if isinstance(ens, dict):
        v = ens.get("p50_high_f")
        if v is None:
            v = ens.get("mean_high_f")
        if v is not None:
            try:
                out["GFS-ens"] = round(float(v), 1)
            except (TypeError, ValueError):
                pass
    return out


def _forecast_f_in_bucket(model_high_f: float, winners: list) -> Optional[bool]:
    """True if the model's °F high lands in any winning bucket.

    `winners` is a list of (bucket_unit, bucket_min, bucket_max). Returns None
    when there is no winning bucket recorded yet (market unresolved).
    """
    if not winners:
        return None
    for unit, bmin, bmax in winners:
        val = (model_high_f - 32.0) * 5.0 / 9.0 if unit == "C" else model_high_f
        if temp_in_bucket(bmin, bmax, val):
            return True
    return False


def _score_model_forecasts(model_fc: dict, winners: list) -> dict:
    """Annotate each model forecast with correctness vs the winning bucket(s).

    Returns {label → {"f": high_f, "correct": bool|None}}.
    """
    out: dict = {}
    for label, high_f in model_fc.items():
        out[label] = {"f": high_f, "correct": _forecast_f_in_bucket(high_f, winners)}
    return out


async def _winning_bounds_map(db, market_ids: set) -> dict:
    """market_id → list of (bucket_unit, bucket_min, bucket_max) for won buckets."""
    if not market_ids:
        return {}
    rows = (await db.execute(
        select(MarketOutcome).where(
            MarketOutcome.market_id.in_(market_ids),
            MarketOutcome.won == True,
        )
    )).scalars().all()
    out: dict = {}
    for oc in rows:
        out.setdefault(oc.market_id, []).append(
            (resolve_bucket_unit(oc), oc.bucket_min, oc.bucket_max)
        )
    return out


# Per-range confidence bands (not cumulative) for the Stats breakdown. Each
# position falls into exactly one band, so summing every band reproduces the
# panel totals. Cumulative thresholds were dropped because they produced
# duplicate rows whenever positions cluster in a narrow range (the bot only
# buys high-confidence opps, so 50%–90% all showed identical numbers).
_CONF_BANDS: list[tuple[int, int]] = [
    (0, 49), (50, 59), (60, 69), (70, 74), (75, 79),
    (80, 84), (85, 89), (90, 94), (95, 100),
]


def _band_stats(subset: list) -> dict:
    """Aggregate P&L stats for a slice of position rows.

    Each row must expose .virtual_status, .virtual_cost, .virtual_pnl.
    "Settled" = win/loss only; invested/net_pnl/roi are computed over those.
    `positions` counts every row in the slice (open + settled).
    """
    settled = [r for r in subset if r.virtual_status in ("win", "loss")]
    w = sum(1 for r in settled if r.virtual_status == "win")
    l = sum(1 for r in settled if r.virtual_status == "loss")
    cost = sum(float(r.virtual_cost or 0) for r in settled)
    pnl = sum(float(r.virtual_pnl or 0) for r in settled)
    n_settled = w + l
    return {
        "positions": len(subset),
        "wins": w,
        "losses": l,
        "win_rate_pct": round(w / n_settled * 100, 1) if n_settled else None,
        "invested": round(cost, 2),
        "net_pnl": round(pnl, 2),
        "roi_pct": round(pnl / cost * 100, 1) if cost > 0 else None,
    }


def _confidence_band_breakdown(rows: list) -> list:
    """Group already-filtered position rows into per-confidence-band stats.

    `rows` must each expose .confidence_score plus the fields _band_stats needs.
    Callers pass the SAME filtered row set the headline cards are computed from
    (date/city/min-conf), which guarantees the band rows reconcile with the
    panel: summing positions / invested / net_pnl across bands reproduces the
    headline totals exactly. Empty bands are omitted so the table has no blank
    rows.
    """
    out: list = []
    for lo, hi in _CONF_BANDS:
        subset = [r for r in rows if lo <= (r.confidence_score or 0) <= hi]
        if not subset:
            continue
        label = f"{lo}–{hi}%" if hi < 100 else f"{lo}–100%"
        out.append({"band": label, "min_conf": lo, "max_conf": hi, **_band_stats(subset)})
    return out


@router.get("/stats")
async def admin_stats(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    from_date: Optional[str] = Query(default=None, description="ISO YYYY-MM-DD"),
    to_date: Optional[str] = Query(default=None, description="ISO YYYY-MM-DD; inclusive"),
    city_id: Optional[int] = Query(default=None),
    date_field: str = Query(default="detected", description="'detected' or 'event'"),
    min_confidence: Optional[int] = Query(default=None, description="Minimum confidence score 0-100"),
):
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None
    by_event = date_field == "event"
    from_ev = from_dt.date() if (from_dt and by_event) else None
    to_ev = to_dt_inc.date() if (to_dt_inc and by_event) else None

    def opp_q(base, min_conf: Optional[int] = None):
        """Apply date/city filters (and optional confidence floor) to a query."""
        q = base
        # event_date or city filtering needs the Market join (1:1, no inflation).
        if by_event or city_id is not None:
            q = q.join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id) \
                 .join(Market, Market.id == MarketOutcome.market_id)
        if by_event:
            if from_ev is not None:
                q = q.where(Market.event_date >= from_ev)
            if to_ev is not None:
                q = q.where(Market.event_date <= to_ev)
        else:
            if from_dt is not None:
                q = q.where(Opportunity.detected_at >= from_dt)
            if to_dt is not None:
                q = q.where(Opportunity.detected_at < to_dt)
        if city_id is not None:
            q = q.where(Market.city_id == city_id)
        if min_conf is not None:
            q = q.where(Opportunity.confidence_score >= min_conf)
        return q

    # Helper so callers that always pass min_confidence don't need to repeat it.
    def fq(base):
        return opp_q(base, min_conf=min_confidence)

    total_opps = (await db.execute(fq(select(func.count(Opportunity.id))))).scalar() or 0
    alerted = (await db.execute(
        fq(select(func.count(Opportunity.id))).where(Opportunity.alert_sent == True)
    )).scalar() or 0
    wins = (await db.execute(
        fq(select(func.count(Opportunity.id))).where(Opportunity.outcome == "WIN")
    )).scalar() or 0
    losses = (await db.execute(
        fq(select(func.count(Opportunity.id))).where(Opportunity.outcome == "LOSS")
    )).scalar() or 0
    open_pos = (await db.execute(
        fq(select(func.count(Opportunity.id)))
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
        fq(select(func.count(Opportunity.id)))
        .where(Opportunity.virtual_shares != None)
    )).scalar() or 0
    # Cost of CLOSED positions only (win + loss) — used together with total_payout
    # and net_pnl so the three numbers are internally consistent:
    #   net_pnl = total_payout - total_cost  (within closed positions)
    total_cost = (await db.execute(
        fq(select(func.coalesce(func.sum(Opportunity.virtual_cost), 0.0)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar() or 0.0
    # Cost of still-open positions = money at risk, not yet resolved
    open_cost = (await db.execute(
        fq(select(func.coalesce(func.sum(Opportunity.virtual_cost), 0.0)))
        .where(Opportunity.virtual_status == "open")
    )).scalar() or 0.0
    total_payout = (await db.execute(
        fq(select(func.coalesce(func.sum(Opportunity.virtual_payout), 0.0)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar() or 0.0
    net_pnl = (await db.execute(
        fq(select(func.coalesce(func.sum(Opportunity.virtual_pnl), 0.0)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar() or 0.0
    pos_wins = (await db.execute(
        fq(select(func.count(Opportunity.id)))
        .where(Opportunity.virtual_status == "win")
    )).scalar() or 0
    pos_losses = (await db.execute(
        fq(select(func.count(Opportunity.id)))
        .where(Opportunity.virtual_status == "loss")
    )).scalar() or 0
    pos_win_rate = round(pos_wins / max(pos_wins + pos_losses, 1) * 100, 1)
    # avg_cost across all opened positions (closed + open)
    avg_cost = (float(total_cost + open_cost) / positions_opened) if positions_opened else 0.0
    best_pnl = (await db.execute(
        fq(select(func.max(Opportunity.virtual_pnl)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar()
    worst_pnl = (await db.execute(
        fq(select(func.min(Opportunity.virtual_pnl)))
        .where(Opportunity.virtual_status.in_(["win", "loss"]))
    )).scalar()

    # ── Performance by confidence band ──────────────────────────────────────
    # Honours the SAME filters as the cards above (date/city/min-conf) so the
    # band rows always reconcile with the headline panel — see
    # _confidence_band_breakdown for the full rationale. One SQL round-trip;
    # grouped into bands in Python.
    band_rows = (await db.execute(
        fq(
            select(
                Opportunity.confidence_score,
                Opportunity.virtual_status,
                Opportunity.virtual_cost,
                Opportunity.virtual_pnl,
            )
        ).where(Opportunity.virtual_shares != None)
    )).all()
    by_confidence_band = _confidence_band_breakdown(band_rows)

    return {
        "filter": {
            "from_date": from_date,
            "to_date": to_date,
            "city_id": city_id,
            "min_confidence": min_confidence,
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
        # Per-band breakdown: each row = positions with confidence in
        # [min_conf, max_conf]. Honours the same filters as the cards above
        # (date/city/min-conf), so the band rows always reconcile with the
        # headline panel. Only non-empty bands are included.
        "by_confidence_band": by_confidence_band,
    }


@router.get("/model-accuracy")
async def admin_model_accuracy(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    city_id: Optional[int] = Query(default=None),
    date_field: str = Query(default="detected", description="'detected' or 'event'"),
):
    """Per-model forecast accuracy over settled markets.

    For every resolved opportunity we compare each weather model's captured
    point forecast against the bucket Polymarket actually settled (MarketOutcome.won).
    A model is "correct" for that opportunity when its predicted daily high lands
    in the winning bucket. Results are aggregated overall and per city so we can
    see which models perform best where (and potentially weight them per city).
    """
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None
    by_event = date_field == "event"

    q = (
        select(Opportunity, Market.id, City.name)
        .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
        .join(Market, Market.id == MarketOutcome.market_id)
        .join(City, City.id == Market.city_id)
        .where(Market.resolved == True, Opportunity.outcome.in_(["WIN", "LOSS"]))
        # Ordered so the per-(market, model, day) dedup below keeps the LATEST
        # forecast captured that day.
        .order_by(Opportunity.detected_at)
    )
    if by_event:
        if from_dt is not None:
            q = q.where(Market.event_date >= from_dt.date())
        if to_dt_inc is not None:
            q = q.where(Market.event_date <= to_dt_inc.date())
    else:
        if from_dt is not None:
            q = q.where(Opportunity.detected_at >= from_dt)
        if to_dt is not None:
            q = q.where(Opportunity.detected_at < to_dt)
    if city_id is not None:
        q = q.where(Market.city_id == city_id)

    rows = (await db.execute(q)).all()
    winners_map = await _winning_bounds_map(db, {mid for _o, mid, _c in rows})

    # Dedupe per (market, model, detection-day): a market re-detected many
    # times would otherwise be counted once per opportunity row and dominate
    # the accuracy ranking. Rows are ordered by detected_at, so later
    # detections of the same day overwrite earlier ones (latest forecast wins).
    dedup: dict = {}
    for opp, mid, city_name in rows:
        winners = winners_map.get(mid, [])
        if not winners:
            continue  # market resolved but winner bucket not recorded yet
        day = opp.detected_at.date() if opp.detected_at else None
        model_fc = _extract_model_forecasts(opp.signals)
        for label, high_f in model_fc.items():
            dedup[(mid, label, day)] = (label, high_f, winners, city_name)

    # tally[label] = [correct, total]; city_tally[city][label] = [correct, total]
    tally: dict = {}
    city_tally: dict = {}
    for label, high_f, winners, city_name in dedup.values():
        correct = _forecast_f_in_bucket(high_f, winners)
        if correct is None:
            continue
        t = tally.setdefault(label, [0, 0])
        t[1] += 1
        if correct:
            t[0] += 1
        ct = city_tally.setdefault(city_name or "—", {}).setdefault(label, [0, 0])
        ct[1] += 1
        if correct:
            ct[0] += 1

    def _rank(d: dict) -> list:
        items = [
            {
                "model": label,
                "correct": c,
                "total": n,
                "pct": round(100 * c / n, 1) if n else None,
            }
            for label, (c, n) in d.items()
        ]
        items.sort(key=lambda x: (x["pct"] if x["pct"] is not None else -1), reverse=True)
        return items

    by_city = []
    for cname, models in sorted(city_tally.items()):
        ranked = _rank(models)
        best = ranked[0] if ranked else None
        by_city.append({"city": cname, "best_model": best, "models": ranked})

    return {
        "filter": {
            "from_date": from_date, "to_date": to_date,
            "city_id": city_id, "date_field": date_field,
        },
        "overall": _rank(tally),
        "by_city": by_city,
    }


@router.get("/positions")
async def admin_positions(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    city_id: Optional[int] = Query(default=None),
    date_field: str = Query(default="detected", description="'detected' or 'event'"),
    limit: int = Query(default=500, le=5000),
):
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None
    by_event = date_field == "event"

    q = (
        select(Opportunity, MarketOutcome, Market, City)
        .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
        .join(Market, Market.id == MarketOutcome.market_id)
        .join(City, City.id == Market.city_id)
        .where(Opportunity.virtual_shares != None)
        .order_by(desc(Opportunity.detected_at))
        .limit(limit)
    )
    if by_event:
        if from_dt is not None:
            q = q.where(Market.event_date >= from_dt.date())
        if to_dt_inc is not None:
            q = q.where(Market.event_date <= to_dt_inc.date())
    else:
        if from_dt is not None:
            q = q.where(Opportunity.detected_at >= from_dt)
        if to_dt is not None:
            q = q.where(Opportunity.detected_at < to_dt)
    if city_id is not None:
        q = q.where(Market.city_id == city_id)

    rows = (await db.execute(q)).all()
    winners_map = await _winning_bounds_map(db, {m.id for _o, _oc, m, _c in rows})
    out = []
    for opp, oc, market, city in rows:
        model_fc = _extract_model_forecasts(opp.signals)
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
            # Per-model forecasts captured at detection time, scored against the
            # winning bucket once the market settled (correct=None if unresolved).
            "model_forecasts": _score_model_forecasts(
                model_fc, winners_map.get(market.id, [])
            ),
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
    limit: int = Query(default=500, le=5000),
    only_alerted: bool = Query(default=False),
    outcome: Optional[str] = Query(default=None),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    city_id: Optional[int] = Query(default=None),
    date_field: str = Query(default="detected", description="'detected' or 'event'"),
):
    from_dt = _parse_iso_date(from_date, "from_date")
    to_dt_inc = _parse_iso_date(to_date, "to_date")
    to_dt = (to_dt_inc + timedelta(days=1)) if to_dt_inc else None
    by_event = date_field == "event"

    # Single joined query for every row's outcome/market/city. The previous
    # version issued 3 extra queries PER opportunity (an N+1 that became a
    # timeout risk once the row cap was raised so date-range listings are
    # complete). The outcome→market→city chain is 1:1, so the joins never
    # inflate the row count.
    q = (
        select(Opportunity, MarketOutcome, Market, City)
        .join(MarketOutcome, MarketOutcome.id == Opportunity.outcome_id)
        .join(Market, Market.id == MarketOutcome.market_id)
        .join(City, City.id == Market.city_id)
        .order_by(desc(Opportunity.detected_at))
        .limit(limit)
    )
    if only_alerted:
        q = q.where(Opportunity.alert_sent == True)
    if outcome:
        q = q.where(Opportunity.outcome == outcome.upper())
    if city_id is not None:
        q = q.where(Market.city_id == city_id)
    if by_event:
        if from_dt is not None:
            q = q.where(Market.event_date >= from_dt.date())
        if to_dt_inc is not None:
            q = q.where(Market.event_date <= to_dt_inc.date())
    else:
        if from_dt is not None:
            q = q.where(Opportunity.detected_at >= from_dt)
        if to_dt is not None:
            q = q.where(Opportunity.detected_at < to_dt)

    rows = (await db.execute(q)).all()
    winners_map = await _winning_bounds_map(db, {m.id for _o, _oc, m, _c in rows})
    out = []
    for opp, oc, market, city in rows:
        out.append({
            "id": opp.id,
            "detected_at": opp.detected_at.isoformat() if opp.detected_at else None,
            "city": city.name if city else None,
            "market": market.question if market else None,
            "market_url": (
                f"https://polymarket.com/event/{market.external_id}"
                if market and market.external_id else None
            ),
            "event_date": (
                market.event_date.isoformat()
                if market and market.event_date else None
            ),
            "bucket": oc.bucket_label if oc else None,
            "side": opp.side,
            "market_price": float(opp.market_price) if opp.market_price is not None else None,
            "true_prob": float(opp.estimated_true_prob) if opp.estimated_true_prob is not None else None,
            "edge": float(opp.edge) if opp.edge is not None else None,
            "confidence": opp.confidence_score,
            "alert_sent": opp.alert_sent,
            "outcome": opp.outcome,
            "closed_at": opp.closed_at.isoformat() if opp.closed_at else None,
            # Per-model forecasts captured at detection time, scored against the
            # winning bucket once the market settles (correct=None if unresolved).
            "model_forecasts": _score_model_forecasts(
                _extract_model_forecasts(opp.signals), winners_map.get(market.id, [])
            ),
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
            "onshore_wind_dir": getattr(c, "onshore_wind_dir", None),
            "active": c.active,
            "blacklisted": bool(getattr(c, "blacklisted", False)),
            "suspended_until": c.suspended_until.isoformat() if getattr(c, "suspended_until", None) else None,
            "suspension_reason": getattr(c, "suspension_reason", None),
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
    if "onshore_wind_dir" in payload:
        v = payload["onshore_wind_dir"]
        if v is None or v == "":
            city.onshore_wind_dir = None
        else:
            v = int(v)
            if not (0 <= v <= 359):
                raise HTTPException(400, "onshore_wind_dir must be 0-359 (compass degrees)")
            city.onshore_wind_dir = v
    if "suspended_until" in payload:
        v = payload["suspended_until"]
        if v is None or v == "":
            city.suspended_until = None
            city.suspension_reason = None
        else:
            from datetime import datetime, timezone
            try:
                city.suspended_until = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if city.suspended_until.tzinfo is None:
                    city.suspended_until = city.suspended_until.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(400, "suspended_until must be an ISO-8601 datetime")
    if "suspension_reason" in payload:
        city.suspension_reason = payload["suspension_reason"] or None
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
