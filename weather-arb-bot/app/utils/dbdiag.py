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


async def database_size(
    db, limit: int = 20, exact_counts: bool = False, exact_limit: int = 5
) -> dict:
    """Total DB size + the largest tables, with bloat detection. Never raises.

    exact_counts=True REFRESHES the row statistics for the `exact_limit` biggest
    tables (ANALYZE + pg_class.reltuples) instead of trusting stale pg_stat
    numbers, which can be wildly wrong — market_prices once reported 3,561 rows
    when it held 17.5M. ANALYZE samples a bounded number of pages, so this stays
    cheap; a literal COUNT(*) would read the entire multi-GB table on every call.
    """
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
        # NOTE: `live` is pg_stat's ESTIMATE and can be wildly wrong — in
        # production market_prices reported 3,561 live rows while COUNT(*)
        # returned 17,532,604. Deriving bloat from it produced a false
        # "BLOATED" alarm on tables that were simply full of real data, so the
        # estimate is NEVER used to declare bloat. A verdict is only issued
        # once exact_counts has supplied a real number (filled in below).
        bpr = (total_b / live) if live > 0 else None
        is_bloated = False
        tables.append({
            "table": r.table_name,
            "total_mb": _mb(r.total_bytes),
            "table_mb": _mb(r.table_bytes),
            "index_mb": _mb(r.index_bytes),
            "live_rows": live,
            "dead_rows": dead,
            "bytes_per_row": round(bpr) if bpr else None,
            "bytes_per_row_is_estimate": True,
            "bloated": is_bloated,
            "_total_bytes": total_b,
            "last_vacuum": r.last_vacuum.isoformat() if r.last_vacuum else None,
            "last_autovacuum": r.last_autovacuum.isoformat() if r.last_autovacuum else None,
        })
    out["tables"] = tables
    out["dead_rows_total"] = dead_total
    out["bloated_tables"] = bloated

    # live_rows above comes from pg_stat (an ESTIMATE that ANALYZE refreshes and
    # that can read 0 right after a vacuum). When the caller needs certainty —
    # e.g. before deciding whether a 3 GB table actually holds anything — run an
    # exact COUNT(*) on the biggest tables. Opt-in: a seq scan over a bloated
    # table reads every page, so it is slow on a large file.
    if exact_counts:
        exact: dict = {}
        for t in tables[:exact_limit]:
            name = t["table"]
            try:
                # Table names come from the catalog, not user input.
                # ANALYZE + reltuples instead of COUNT(*).
                #
                # COUNT(*) sequentially scans the WHOLE table: on market_prices
                # that reads ~3 GB from disk into the page cache on every click,
                # which measurably drove up the Railway bill during diagnosis.
                # ANALYZE samples a bounded number of pages (fast, light lock)
                # and refreshes pg_class.reltuples, giving a count accurate to a
                # few percent — far more than enough to tell 3,561 apart from
                # 17.5M, which is the whole point of this readout.
                await db.execute(text(f'ANALYZE "{name}"'))
                n = int((await db.execute(
                    text("SELECT reltuples::bigint FROM pg_class WHERE relname = :n"),
                    {"n": name},
                )).scalar_one())
                exact[name] = n
                t["exact_rows"] = n
                # Recompute bytes-per-row from the TRUE count, and only now
                # decide bloat. A table storing real rows at a sane size is not
                # bloated no matter what pg_stat estimated.
                total_b = t.pop("_total_bytes", 0)
                if n > 0:
                    real_bpr = total_b / n
                    t["bytes_per_row"] = round(real_bpr)
                    t["bytes_per_row_is_estimate"] = False
                    if total_b >= _BLOAT_MIN_BYTES and real_bpr > _BLOAT_BYTES_PER_ROW:
                        t["bloated"] = True
                        bloated.append(name)
                elif total_b >= _BLOAT_MIN_BYTES:
                    # Genuinely empty yet occupying real space → true bloat.
                    t["bytes_per_row"] = None
                    t["bytes_per_row_is_estimate"] = False
                    t["bloated"] = True
                    bloated.append(name)
            except Exception as e:
                logger.warning(f"[dbdiag] count failed for {name}: {e}")
                exact[name] = None
        out["exact_rows"] = exact
        out["bloated_tables"] = bloated
        out["vacuum_full_recommended"] = bool(bloated) or dead_total > 100_000

    for t in tables:
        t.pop("_total_bytes", None)
    # Recommend VACUUM FULL on EITHER signal: lots of dead tuples, or bloated
    # files whose space autovacuum already freed internally but never released.
    out["vacuum_full_recommended"] = bool(bloated) or dead_total > 100_000
    return out
