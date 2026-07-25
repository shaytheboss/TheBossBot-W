"""Database size diagnostics.

The Railway cost is driven by the Postgres service (the Python app measures only
~360 MB, while the billed graph showed ~4 GB and carries the Volume metric).
Postgres grows its cache with the data it holds, so the only real lever is making
the database smaller — and to do that we must first SEE where the space is.

This module answers three questions with one cheap, read-only query:
  1. How big is the database in total?
  2. Which tables dominate (table bytes vs index bytes)?
  3. How much is DEAD tuples — i.e. space already deleted (e.g. by the forecast
     de-dup) that Postgres has not returned to disk yet, which is exactly what
     VACUUM FULL reclaims.

Postgres-specific; degrades gracefully (returns an error field) on other engines.
"""
import logging
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Per-table sizes + live/dead tuple counts. pg_stat_user_tables may lack a row
# for a never-touched table, hence the LEFT JOIN.
_TABLE_SIZE_SQL = """
SELECT
    c.relname                             AS table_name,
    pg_total_relation_size(c.oid)         AS total_bytes,
    pg_relation_size(c.oid)               AS table_bytes,
    pg_indexes_size(c.oid)                AS index_bytes,
    COALESCE(s.n_live_tup, 0)             AS live_rows,
    COALESCE(s.n_dead_tup, 0)             AS dead_rows,
    s.last_vacuum,
    s.last_autovacuum
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT :limit
"""


def _mb(v: Optional[int]) -> Optional[float]:
    return round(v / 1024 / 1024, 1) if v is not None else None


async def database_size(db, limit: int = 20) -> dict:
    """Total DB size + the largest tables, with dead-tuple bloat. Never raises."""
    out: dict = {}
    try:
        total = (await db.execute(
            text("SELECT pg_database_size(current_database())")
        )).scalar_one()
        out["total_mb"] = _mb(total)
    except Exception as e:
        logger.warning(f"[dbdiag] size query failed: {e}")
        return {"error": f"{type(e).__name__}: {e}"}

    try:
        rows = (await db.execute(text(_TABLE_SIZE_SQL), {"limit": limit})).all()
    except Exception as e:
        logger.warning(f"[dbdiag] table query failed: {e}")
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    tables = []
    dead_total = 0
    for r in rows:
        dead_total += int(r.dead_rows or 0)
        tables.append({
            "table": r.table_name,
            "total_mb": _mb(r.total_bytes),
            "table_mb": _mb(r.table_bytes),
            "index_mb": _mb(r.index_bytes),
            "live_rows": int(r.live_rows or 0),
            "dead_rows": int(r.dead_rows or 0),
            "last_vacuum": r.last_vacuum.isoformat() if r.last_vacuum else None,
            "last_autovacuum": r.last_autovacuum.isoformat() if r.last_autovacuum else None,
        })
    out["tables"] = tables
    out["dead_rows_total"] = dead_total
    # A large dead-row count means deleted space is still occupying disk and
    # cache — VACUUM FULL would return it to the OS.
    out["vacuum_full_recommended"] = dead_total > 100_000
    return out
