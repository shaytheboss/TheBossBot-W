from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.alert import TelegramUser

router = APIRouter()


class UserPreferences(BaseModel):
    cities_watched: Optional[List[int]] = None
    min_confidence: Optional[int] = None
    alert_types_enabled: Optional[List[str]] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None


@router.post("/{chat_id}/preferences")
async def update_preferences(chat_id: int, body: UserPreferences, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TelegramUser).where(TelegramUser.chat_id == chat_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found — send /start to the bot first")
    if body.cities_watched is not None:
        user.cities_watched = body.cities_watched
    if body.min_confidence is not None:
        user.min_confidence = body.min_confidence
    if body.alert_types_enabled is not None:
        user.alert_types_enabled = body.alert_types_enabled
    await db.commit()
    return {"status": "updated"}
