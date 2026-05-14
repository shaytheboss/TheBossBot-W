from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings


def _async_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


# SQL echo is OFF by default — it was previously enabled in non-production
# environments which flooded Railway logs (500+ events/sec rate limit hit).
# Re-enable per session via SQLALCHEMY_ECHO=1 if you genuinely need it.
import os
_ECHO = os.getenv("SQLALCHEMY_ECHO", "").lower() in ("1", "true", "yes")

engine = create_async_engine(
    _async_url(settings.database_url),
    echo=_ECHO,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
