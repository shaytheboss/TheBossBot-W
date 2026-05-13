from fastapi import APIRouter, Depends
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.opportunity import Opportunity

router = APIRouter()


@router.get("")
async def active_opportunities(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Opportunity).where(Opportunity.closed_at == None).order_by(desc(Opportunity.detected_at)))
    return result.scalars().all()


@router.get("/history")
async def opportunity_history(limit: int = 50, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Opportunity).where(Opportunity.closed_at != None).order_by(desc(Opportunity.detected_at)).limit(limit))
    return result.scalars().all()
