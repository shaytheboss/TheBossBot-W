"""An in-memory SQLite database that can host this project's ORM models.

Most tests should use `tests.mocks.db.FakeSession` — it is faster and makes
the expected queries explicit. Reach for this one when the behaviour under
test *is* the SQL: a unique constraint firing, an ordering, a join, a
`DISTINCT ON` rewrite.

Three Postgres-only constructs have to be taught to SQLite first:

    JSONB              → JSON. Identical Python round-trip; SQLite is
                         dynamically typed so the DDL name is accepted as-is.
    ARRAY(Integer)     → JSON list. SQLite has no array type, and
                         `TelegramUser.cities_watched` is a PG array.
    BigInteger PK      → INTEGER. SQLite auto-increments only a column
                         declared exactly `INTEGER PRIMARY KEY`; `BIGINT`
                         inserts NULL and trips the NOT NULL constraint.
                         `Forecast.id` and `MarketPrice.id` are BigInteger.

What this does NOT paper over
-----------------------------
`insert(...).on_conflict_do_nothing(...)` from `sqlalchemy.dialects.postgresql`
will not compile here, and neither will `DISTINCT ON`. That is correct
behaviour, not a gap: those statements are Postgres-specific and a test that
appeared to exercise them on SQLite would be testing a different query than
production runs. Use `FakeSession` for those, or mark the test `integration`.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import JSON

SQLITE_MEMORY = "sqlite+aiosqlite:///:memory:"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return compiler.visit_JSON(JSON(), **kw)


@compiles(PG_ARRAY, "sqlite")
def _compile_array_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return compiler.visit_JSON(JSON(), **kw)


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    # Emitting INTEGER is what makes `id BIGSERIAL` behave like a rowid alias
    # and self-populate. Harmless for the value range a test produces.
    return "INTEGER"


def make_engine(url: str = SQLITE_MEMORY):
    """Build an async SQLite engine sized for a test.

    Note the pool arguments that `app/database.py` passes unconditionally
    (`pool_size`, `max_overflow`) are deliberately absent: SQLite's
    `StaticPool` rejects them. That is why this module builds its own engine
    instead of reusing the application's.
    """
    engine = create_async_engine(url, echo=False, future=True)
    # The @compiles hooks above only rewrite DDL. Binding a Python list to a
    # PG ARRAY column still fails ("type 'list' is not supported"), because
    # the type's bind processor is unchanged. Registering JSON in the
    # dialect's colspecs swaps the whole type implementation, so values
    # round-trip as lists — which is what `TelegramUser.cities_watched` holds.
    engine.dialect.colspecs[PG_ARRAY] = JSON
    return engine


def make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def create_schema(engine, metadata, only: Optional[list] = None) -> None:
    """Create tables. `only` restricts to a list of `Table` objects, which
    keeps a focused test from paying for the whole schema."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all, tables=only)


async def drop_schema(engine, metadata) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
