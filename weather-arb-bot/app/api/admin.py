import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.city import City
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity
from app.models.alert import Alert, TelegramUser
from app.utils.log_buffer import recent_logs
from app.utils.polymarket_discovery import (
    GAMMA_API, build_all_candidates, fetch_event_by_slug, fetch_events_by_tag,
)

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
    """Trigger Polymarket market discovery immediately. Returns the run stats."""
    from app.workers.jobs import job_discover_markets
    stats = await job_discover_markets(notify=False)
    return {"ok": True, **stats}


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


@router.get("/stats")
async def admin_stats(_: str = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    total_opps = (await db.execute(select(func.count(Opportunity.id)))).scalar() or 0
    alerted = (
        await db.execute(select(func.count(Opportunity.id)).where(Opportunity.alert_sent == True))
    ).scalar() or 0
    wins = (
        await db.execute(select(func.count(Opportunity.id)).where(Opportunity.outcome == "WIN"))
    ).scalar() or 0
    losses = (
        await db.execute(select(func.count(Opportunity.id)).where(Opportunity.outcome == "LOSS"))
    ).scalar() or 0
    open_pos = (
        await db.execute(
            select(func.count(Opportunity.id)).where(
                Opportunity.alert_sent == True, Opportunity.outcome == None
            )
        )
    ).scalar() or 0

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

    return {
        "opportunities": {
            "total": total_opps,
            "alerted": alerted,
            "open": open_pos,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
        },
        "inventory": {
            "cities": cities,
            "markets": markets,
            "outcomes": outcomes,
            "outcomes_with_token": outcomes_with_token,
            "telegram_users": telegram_users,
        },
    }


@router.get("/opportunities")
async def admin_opportunities(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, le=500),
    only_alerted: bool = Query(default=False),
    outcome: Optional[str] = Query(default=None),
):
    q = select(Opportunity).order_by(desc(Opportunity.detected_at)).limit(limit)
    if only_alerted:
        q = q.where(Opportunity.alert_sent == True)
    if outcome:
        q = q.where(Opportunity.outcome == outcome.upper())
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
        }
        for c in rows
    ]


@router.post("/cities", status_code=201)
async def admin_city_create(
    body: CityCreateIn,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new monitored city. `wunderground_url` defaults to empty string
    if not provided, since the City model requires it as NOT NULL."""
    payload = body.model_dump()
    # City.wunderground_url is NOT NULL but admin UI doesn't always know it.
    if not payload.get("wunderground_url"):
        payload["wunderground_url"] = ""
    payload["primary_icao"] = payload["primary_icao"].strip().upper()
    if payload.get("reference_icao"):
        payload["reference_icao"] = payload["reference_icao"].strip().upper()
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
        "metar_fetch_interval": settings.metar_fetch_interval,
        "polymarket_fetch_interval": settings.polymarket_fetch_interval,
        "analyzer_run_interval": settings.analyzer_run_interval,
        "alert_dedup_minutes": settings.alert_dedup_minutes,
        "app_env": settings.app_env,
    }


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
    logger.info(
        f"Admin updated thresholds: min_conf={settings.min_confidence_for_alert} "
        f"min_edge={settings.min_edge_for_alert}"
    )
    return {"ok": True}


@router.get("/logs")
async def admin_logs(
    _: str = Depends(require_admin),
    limit: int = Query(default=200, le=500),
    level: Optional[str] = Query(default=None),
):
    return {"logs": recent_logs(limit=limit, level=level)}
