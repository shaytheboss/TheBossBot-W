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


# Bloat heuristic. Ignore small tables; above that, a table whose average
# bytes-per-live-row is huge (or that reports no live rows at all while holding
# real space) is carrying free-but-unreleased space that only VACUUM FULL
# returns to the OS.
_BLOAT_MIN_BYTES = 50 * 1024 * 1024      # only consider tables >= 50 MB
_BLOAT_BYTES_PER_ROW = 5_000             # >5 KB per row is far above any real row


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
    bloated = []
    for r in rows:
        dead = int(r.dead_rows or 0)
        live = int(r.live_rows or 0)
        total_b = int(r.total_bytes or 0)
        dead_total += dead
        # Bytes per live row exposes bloat that dead-row counts CANNOT see:
        # after a big delete, autovacuum clears the dead tuples (dead_rows→0) but
        # the file stays large — the space is merely marked reusable, never
        # returned to the OS. Only VACUUM FULL rewrites the file. A market_prices
        # row (6 numbers) should be ~100 bytes; tens of KB/row means bloat.
        bpr = (total_b / live) if live > 0 else None
        is_bloated = total_b >= _BLOAT_MIN_BYTES and (live == 0 or (bpr or 0) > _BLOAT_BYTES_PER_ROW)
        if is_bloated:
            bloated.append(r.table_name)
        tables.append({
            "table": r.table_name,
            "total_mb": _mb(r.total_bytes),
            "table_mb": _mb(r.table_bytes),
            "index_mb": _mb(r.index_bytes),
            "live_rows": live,
            "dead_rows": dead,
            "bytes_per_row": round(bpr) if bpr else None,
            "bloated": is_bloated,
            "last_vacuum": r.last_vacuum.isoformat() if r.last_vacuum else None,
            "last_autovacuum": r.last_autovacuum.isoformat() if r.last_autovacuum else None,
        })
    out["tables"] = tables
    out["dead_rows_total"] = dead_total
    out["bloated_tables"] = bloated
    # Recommend VACUUM FULL on EITHER signal: lots of dead tuples, or bloated
    # files whose space autovacuum already freed internally but never released.
    out["vacuum_full_recommended"] = bool(bloated) or dead_total > 100_000
    return out
