from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.city import City
from app.models.metar import MetarObservation
from app.models.forecast import Forecast
from app.analyzers.signal_aggregator import SignalAggregator
from app.models.market import Market, MarketOutcome

router = APIRouter()
aggregator = SignalAggregator()


class CityCreate(BaseModel):
    name: str
    primary_icao: str
    reference_icao: Optional[str] = None
    wunderground_url: str
    nws_lat: Optional[float] = None
    nws_lon: Optional[float] = None
    timezone: str = "America/Los_Angeles"
    buoy_id: Optional[str] = None


class CityUpdate(CityCreate):
    active: Optional[bool] = None


@router.get("")
async def list_cities(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(City).order_by(City.name))
    return result.scalars().all()


@router.post("", status_code=201)
async def create_city(body: CityCreate, db: AsyncSession = Depends(get_db)):
    city = City(**body.model_dump())
    db.add(city)
    await db.commit()
    await db.refresh(city)
    return city


@router.put("/{city_id}")
async def update_city(city_id: int, body: CityUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(City).where(City.id == city_id))
    city = result.scalar_one_or_none()
    if not city:
        raise HTTPException(404, "City not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(city, k, v)
    await db.commit()
    return city


@router.delete("/{city_id}", status_code=204)
async def delete_city(city_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(City).where(City.id == city_id))
    city = result.scalar_one_or_none()
    if not city:
        raise HTTPException(404, "City not found")
    await db.delete(city)
    await db.commit()


@router.get("/{city_id}/current")
async def city_current(city_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(City).where(City.id == city_id))
    city = result.scalar_one_or_none()
    if not city:
        raise HTTPException(404, "City not found")
    metar_result = await db.execute(select(MetarObservation).where(MetarObservation.icao == city.primary_icao).order_by(desc(MetarObservation.observed_at)).limit(1))
    forecast_result = await db.execute(select(Forecast).where(Forecast.city_id == city_id, Forecast.source == "wunderground").order_by(desc(Forecast.retrieved_at)).limit(1))
    return {"city": city, "latest_metar": metar_result.scalar_one_or_none(), "latest_forecast": forecast_result.scalar_one_or_none()}


@router.get("/{city_id}/history")
async def city_history(city_id: int, from_dt: Optional[datetime] = None, to_dt: Optional[datetime] = None, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(City).where(City.id == city_id))
    city = result.scalar_one_or_none()
    if not city:
        raise HTTPException(404, "City not found")
    if not from_dt:
        from_dt = datetime.now(timezone.utc) - timedelta(hours=24)
    if not to_dt:
        to_dt = datetime.now(timezone.utc)
    metar_result = await db.execute(select(MetarObservation).where(MetarObservation.icao == city.primary_icao, MetarObservation.observed_at >= from_dt, MetarObservation.observed_at <= to_dt).order_by(MetarObservation.observed_at))
    return metar_result.scalars().all()


@router.get("/{city_id}/signals")
async def city_signals(city_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(City).where(City.id == city_id))
    city = result.scalar_one_or_none()
    if not city:
        raise HTTPException(404, "City not found")
    outcome_result = await db.execute(select(MarketOutcome).join(Market).where(Market.city_id == city_id, Market.resolved == False).limit(1))
    outcome = outcome_result.scalar_one_or_none()
    if not outcome:
        raise HTTPException(404, "No active market outcomes for this city")
    return await aggregator.aggregate(db, city_id, city.primary_icao, city.reference_icao, outcome)
