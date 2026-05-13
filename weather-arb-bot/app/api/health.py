from fastapi import APIRouter
from sqlalchemy import text
from app.database import AsyncSessionLocal

router = APIRouter()


@router.get("/health")
async def health():
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "degraded", "db": str(e)}
