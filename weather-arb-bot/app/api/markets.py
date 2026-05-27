from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.market import Market, MarketOutcome, MarketPrice
from app.analyzers.signal_aggregator import SignalAggregator
from app.analyzers.probability_estimator import estimate_true_probability
from app.analyzers.confidence_scorer import compute_confidence
from app.models.city import City
from app.utils.units import resolve_bucket_unit

router = APIRouter()
aggregator = SignalAggregator()


class MarketCreate(BaseModel):
    city_id: int
    external_id: str
    platform: str = "polymarket"
    question: str
    resolution_source: Optional[str] = None
    event_date: str
    resolution_time: Optional[datetime] = None


class OutcomeCreate(BaseModel):
    market_id: int
    bucket_label: str
    bucket_min: Optional[int] = None
    bucket_max: Optional[int] = None
    bucket_unit: str = "F"


@router.get("")
async def list_markets(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Market).where(Market.resolved == False).order_by(Market.event_date))
    return result.scalars().all()


@router.get("/{market_id}")
async def get_market(market_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Market).where(Market.id == market_id))
    market = result.scalar_one_or_none()
    if not market:
        raise HTTPException(404, "Market not found")
    outcomes_result = await db.execute(select(MarketOutcome).where(MarketOutcome.market_id == market_id))
    return {"market": market, "outcomes": outcomes_result.scalars().all()}


@router.get("/{market_id}/prices")
async def market_prices(market_id: int, hours: int = 24, db: AsyncSession = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    outcomes_result = await db.execute(select(MarketOutcome).where(MarketOutcome.market_id == market_id))
    outcomes = outcomes_result.scalars().all()
    data = {}
    for outcome in outcomes:
        prices_result = await db.execute(select(MarketPrice).where(MarketPrice.outcome_id == outcome.id, MarketPrice.timestamp >= since).order_by(MarketPrice.timestamp))
        data[outcome.bucket_label] = prices_result.scalars().all()
    return data


@router.get("/{market_id}/analysis")
async def market_analysis(market_id: int, db: AsyncSession = Depends(get_db)):
    market_result = await db.execute(select(Market).where(Market.id == market_id))
    market = market_result.scalar_one_or_none()
    if not market:
        raise HTTPException(404, "Market not found")
    city_result = await db.execute(select(City).where(City.id == market.city_id))
    city = city_result.scalar_one_or_none()
    outcomes_result = await db.execute(select(MarketOutcome).where(MarketOutcome.market_id == market_id))
    outcomes = outcomes_result.scalars().all()
    analysis = []
    for outcome in outcomes:
        bucket_unit = resolve_bucket_unit(outcome)
        signals = await aggregator.aggregate(
            db, market.city_id,
            city.primary_icao if city else "KSFO",
            city.reference_icao if city else None,
            outcome,
            forecast_date=market.event_date,
        )
        true_prob = estimate_true_probability(
            signals, outcome.bucket_min, outcome.bucket_max, bucket_unit=bucket_unit
        )
        confidence = compute_confidence(
            signals, outcome.bucket_min, outcome.bucket_max, bucket_unit=bucket_unit
        )
        market_price = (signals.get("market_price") or {}).get("yes_price")
        edge = (true_prob - market_price) if market_price is not None else None
        analysis.append({
            "bucket": outcome.bucket_label,
            "bucket_unit": bucket_unit,
            "market_price": market_price,
            "true_prob": round(true_prob, 4),
            "confidence": confidence,
            "edge": round(edge, 4) if edge is not None else None,
        })
    return analysis
