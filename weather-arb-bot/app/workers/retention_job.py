"""Data-retention & de-duplication job — the cheap, capability-neutral cost win.

Railway bills mostly on RAM, which climbs as Postgres caches ever-growing
tables. Three sources insert a fresh row on every fetch and nothing ever pruned
them:
  - forecasts       (48 cities x 7 models x 7 days, hourly)  → ~1.9M rows/month
  - market_prices   (every outcome, every 5 min)
  - metar_observations / pireps

This job runs daily and does two things:

1. DE-DUPLICATE forecasts (the big win, ZERO capability loss). The analyzer
   reads only the latest forecast per (city, source, date) and model_skill keeps
   exactly the latest retrieved_at per (source, event_date, days_ahead). So all
   the *intra-day* re-fetches of the same forecast are pure dead weight. We keep
   one row per (city_id, source, forecast_for_date, made-date) — precisely the
   set both readers use — and delete the rest (~24x fewer forecast rows).

2. RETENTION deletes with windows chosen to exceed every computation window, so
   NO feature loses data:
     - forecasts:       keep 120 days (model_skill uses 90)
     - metar:           keep 45 days  (bias_estimator uses 14)
     - market_prices:   keep 45 days  (charts/history only)
     - pireps:          keep 21 days  (same-day signal only)
     - collector_miss:  keep 90 days  (observability)
   Opportunities & alerts are never touched here (the P&L record).

Isolated module (like icon_job/tomorrowio_job) so it can never regress the
existing jobs. Every statement is guarded; a failure logs and moves on.
"""
import logging
from typing import Optional
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.config import settings
from app.database import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)

# Maps a delete-summary key to the physical table it affected, so we VACUUM
# exactly the tables that changed.
_SUMMARY_KEY_TO_TABLE = {
    "forecasts_deduped": "forecasts",
    "forecasts_pruned": "forecasts",
    "forecasts_raw_data_stripped": "forecasts",
    "metar_observations_pruned": "metar_observations",
    "market_prices_pruned": "market_prices",
    "pireps_pruned": "pireps",
    "collector_miss_pruned": "collector_miss",
}


# Every table a retention/de-dup pass can bloat. A one-time VACUUM FULL must
# cover ALL of them — the first version only ever vacuumed `forecasts`, so
# market_prices (measured at 2,963 MB, by far the largest) was never reclaimed.
_VACUUM_FULL_TABLES = [
    "collector_miss", "model_skill", "virtual_exits", "markets",
    "intraday_opportunities", "market_outcomes", "metar_observations",
    "alerts", "opportunities", "pireps", "forecasts", "market_prices",
]


# VACUUM FULL rewrites a table into a NEW file and only drops the old one when
# the rewrite completes, so it transiently needs free disk of roughly the whole
# relation — heap plus every index. market_prices measured 2,754 MB against
# ~750 MB free: Postgres would write until the volume hit 100%, fail, and roll
# back. The rollback is clean, but while the disk is full the bot's own writes
# fail too, which is a far worse outcome than simply not reclaiming the space.
#
# So a table is only rewritten when it comfortably fits. This is a size cap, not
# a free-space check, because the app runs in a different container from
# Postgres and cannot see that volume — the cap is the honest approximation.
VACUUM_FULL_MAX_TABLE_MB = 1_000

_RELATION_SIZE_SQL = """
SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
"""


async def _relation_sizes(conn) -> dict[str, float]:
    """{table: total MB}. Empty on any failure — the caller then skips the
    guard rather than blocking maintenance on a diagnostic query."""
    try:
        rows = (await conn.execute(text(_RELATION_SIZE_SQL))).all()
    except Exception as e:
        logger.warning(f"[retention] relation-size lookup failed: {e}")
        return {}
    return {r.name: (r.bytes or 0) / 1024 / 1024 for r in rows}


def vacuum_targets(deleted: dict, vacuum_full: bool) -> list[str]:
    """Pure helper: which tables to VACUUM after a prune run.

    Routine pass: only the tables that actually had rows removed.

    vacuum_full: ALL known tables, ordered SMALLEST-FIRST. Order matters —
    VACUUM FULL rewrites a table into a new file, so it needs free disk equal to
    the table size. On a nearly-full volume the biggest table can fail for lack
    of space; reclaiming the small ones first frees room for the big one.
    Pure → unit-testable.
    """
    if vacuum_full:
        tables = list(_VACUUM_FULL_TABLES)
        for key, n in (deleted or {}).items():
            t = _SUMMARY_KEY_TO_TABLE.get(key)
            if n and t and t not in tables:
                tables.append(t)
        return tables

    tables: list[str] = []
    for key, n in (deleted or {}).items():
        table = _SUMMARY_KEY_TO_TABLE.get(key)
        if n and table and table not in tables:
            tables.append(table)
    return tables


async def _run_vacuum(
    tables: list[str],
    full: bool = False,
    max_table_mb: Optional[float] = None,
) -> tuple[int, list[str], list[str]]:
    """Run VACUUM on the given tables. Never raises.

    Plain VACUUM (default) does NOT take an exclusive lock — safe on a live DB;
    it reclaims dead tuples for reuse so the table stops bloating. VACUUM FULL
    rewrites the table to a fresh file and RETURNS disk to the OS (shrinks the
    Volume), but takes an exclusive lock for its duration — intended for a
    one-time reclaim right after the first big de-dup.

    VACUUM cannot run inside a transaction, so we use an AUTOCOMMIT engine.

    `max_table_mb` applies to VACUUM FULL only: a relation bigger than this is
    SKIPPED rather than attempted, because the rewrite would fill the volume
    before failing and take the bot's writes down with it. Skipped tables are
    reported, never silently dropped.

    Returns (succeeded_count, errors, skipped). Errors are RETURNED, not just
    logged: the first version swallowed them, so a fully-failed run reported a
    bare "tables_vacuumed: 0" with no way to see why.
    """
    errors: list[str] = []
    skipped: list[str] = []
    if not tables:
        return 0, errors, skipped
    mode = "FULL, ANALYZE" if full else "ANALYZE"
    done = 0
    try:
        ac_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
        async with ac_engine.connect() as conn:
            sizes = await _relation_sizes(conn) if (full and max_table_mb) else {}
            for t in tables:
                size_mb = sizes.get(t)
                if full and max_table_mb and size_mb and size_mb > max_table_mb:
                    skipped.append(f"{t}: {size_mb:.0f} MB > {max_table_mb:.0f} MB cap")
                    logger.warning(
                        f"[retention] skipping VACUUM FULL on {t} ({size_mb:.0f} MB): "
                        f"the rewrite needs that much free disk again and would "
                        f"fill the volume before failing"
                    )
                    continue
                try:
                    await conn.execute(text(f"VACUUM ({mode}) {t}"))
                    done += 1
                except Exception as e:
                    msg = f"{t}: {type(e).__name__}: {e}"
                    errors.append(msg)
                    logger.error(f"[retention] VACUUM {msg}", exc_info=True)
    except Exception as e:
        msg = f"connect: {type(e).__name__}: {e}"
        errors.append(msg)
        logger.error(f"[retention] VACUUM {msg}", exc_info=True)
    return done, errors, skipped


def compute_cutoffs(now: datetime, today: date, cfg) -> dict:
    """Pure helper: retention cutoffs from the config. Testable without a DB.

    `cfg` is any object exposing the *_retention_days attributes (the settings
    object, or a stub in tests). Returns a dict of table → cutoff.
    """
    def d(attr, default):
        return int(getattr(cfg, attr, default))
    return {
        "forecast_date": today - timedelta(days=d("forecast_retention_days", FORECAST_RETENTION_DAYS)),
        "metar_ts": now - timedelta(days=d("metar_retention_days", METAR_RETENTION_DAYS)),
        "market_price_ts": now - timedelta(days=d("market_price_retention_days", MARKET_PRICE_RETENTION_DAYS)),
        "pirep_ts": now - timedelta(days=d("pirep_retention_days", PIREP_RETENTION_DAYS)),
        "collector_miss_ts": now - timedelta(days=d("collector_miss_retention_days", COLLECTOR_MISS_RETENTION_DAYS)),
    }

# Retention windows (days). Each is >= the longest window any feature reads,
# so pruning is capability-neutral. Overridable via settings.
FORECAST_RETENTION_DAYS = 120     # model_skill reads 90
METAR_RETENTION_DAYS = 45         # bias_estimator reads 14
MARKET_PRICE_RETENTION_DAYS = 45
PIREP_RETENTION_DAYS = 21
COLLECTOR_MISS_RETENTION_DAYS = 90

# Only de-dup rows settled for a while, never the freshest write, so a fetch
# happening concurrently with the prune is never touched.
_DEDUP_SETTLE = "interval '90 minutes'"

# ── raw_data stripping ────────────────────────────────────────────────────
# `forecasts.raw_data` holds the provider's untouched payload — ensemble member
# arrays (30-50 floats), NWS period objects with their prose forecasts. Measured
# 2026-09-22: forecasts is 696 MB over 267,471 rows, i.e. ~2.7 KB per row. The
# scalar columns account for roughly 100 bytes of that, so raw_data is ~640 MB,
# about a sixth of the whole database.
#
# It has exactly ONE reader: SignalAggregator._latest_forecast, which pulls the
# ensemble percentiles and the NWS grid identifiers out of it — and only for the
# forecast_for_date currently being analysed, which is never more than
# max_days_ahead_for_alert (3) in the future. Once a target date is in the past
# no code path can ask for it again: model_skill selects four scalar columns
# (PR #101), and no dashboard, admin route or CSV export touches the column.
#
# So clearing raw_data on past dates removes no row and changes no answer. The
# scalar forecast — the thing accuracy scoring and the screens actually read —
# is untouched.
RAW_DATA_KEEP_DAYS = 7            # 2x the 3-day trading horizon, as a margin

# Batching matters here, not just for lock duration. Each UPDATE leaves the old
# row version behind as a dead tuple and writes the change to WAL; rewriting
# 267k TOASTed rows in one statement would spike WAL by more than the free space
# on a volume that is already at 85%. Small committed batches keep the peak flat
# and let autovacuum reclaim as it goes.
_RAW_DATA_BATCH = 2_000
_RAW_DATA_MAX_BATCHES = 200       # ≤400k rows per run — a full pass, bounded


async def _exec_count(db, sql: str, params: dict | None = None) -> int:
    """Run a DELETE and return affected row count; never raises."""
    try:
        result = await db.execute(text(sql), params or {})
        return int(result.rowcount or 0)
    except Exception as e:
        logger.error(f"[retention] statement failed: {e}", exc_info=True)
        await db.rollback()
        return 0


def raw_data_cutoff(today: date, cfg=None) -> date:
    """Target date before which `raw_data` is dead weight. Pure, testable."""
    days = int(getattr(cfg, "raw_data_keep_days", RAW_DATA_KEEP_DAYS)) if cfg else RAW_DATA_KEEP_DAYS
    return today - timedelta(days=days)


async def strip_raw_data(
    db,
    cutoff: date,
    *,
    batch: int = _RAW_DATA_BATCH,
    max_batches: int = _RAW_DATA_MAX_BATCHES,
) -> int:
    """NULL out `forecasts.raw_data` for target dates older than `cutoff`.

    Deletes nothing. Returns the number of rows cleared. Never raises — a
    failure mid-way keeps the batches already committed, which is fine because
    the operation is idempotent and resumes on the next run.
    """
    cleared = 0
    for _ in range(max_batches):
        n = await _exec_count(
            db,
            """
            UPDATE forecasts SET raw_data = NULL
            WHERE id IN (
                SELECT id FROM forecasts
                WHERE forecast_for_date < :cutoff AND raw_data IS NOT NULL
                LIMIT :batch
            )
            """,
            {"cutoff": cutoff, "batch": batch},
        )
        if n == 0:
            break
        await db.commit()
        cleared += n
    if cleared:
        logger.info(
            f"[retention] cleared raw_data on {cleared} forecast rows "
            f"older than {cutoff} (no rows deleted)"
        )
    return cleared


async def job_prune_old_data(vacuum_full: bool = False) -> dict:
    """Daily maintenance. Independent, separately-gated steps:

      • DE-DUP (settings.retention_dedup_enabled, default ON): lossless — removes
        only intra-day duplicate forecast rows no reader ever uses. Safe to run
        without any backup.

      • HARD PRUNE (settings.retention_prune_enabled, default OFF): deletes rows
        older than the retention windows. This DESTROYS historical data, so it
        stays off until backups are in place (see backup_job / archive plan).

    Returns a summary dict for logging/admin.
    """
    dedup_on = bool(getattr(settings, "retention_dedup_enabled", True))
    prune_on = bool(getattr(settings, "retention_prune_enabled", False))
    strip_on = bool(getattr(settings, "retention_strip_raw_data_enabled", True))
    if not dedup_on and not prune_on and not strip_on:
        return {}

    summary: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        # ── 1. De-duplicate forecasts (lossless — keep latest per
        #    city/source/target/made-day, exactly what the readers use) ──────────
        if dedup_on:
            summary["forecasts_deduped"] = await _exec_count(db, f"""
                DELETE FROM forecasts f
                USING (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY city_id, source, forecast_for_date,
                                     (retrieved_at AT TIME ZONE 'UTC')::date
                        ORDER BY retrieved_at DESC
                    ) AS rn
                    FROM forecasts
                    WHERE retrieved_at < now() - {_DEDUP_SETTLE}
                ) d
                WHERE f.id = d.id AND d.rn > 1
            """)
            await db.commit()

        # ── 1.5 Strip raw_data from past target dates (lossless, no deletes) ────
        if strip_on:
            summary["forecasts_raw_data_stripped"] = await strip_raw_data(
                db, raw_data_cutoff(date.today(), settings)
            )

        # ── 2. Retention deletes — OFF by default (destroys history) ────────────
        if prune_on:
            cutoffs = compute_cutoffs(datetime.now(timezone.utc), date.today(), settings)
            for key, table, col in (
                ("forecast_date",     "forecasts",           "forecast_for_date"),
                ("metar_ts",          "metar_observations",  "observed_at"),
                ("market_price_ts",   "market_prices",       "timestamp"),
                ("pirep_ts",          "pireps",              "observed_at"),
                ("collector_miss_ts", "collector_miss",      "detected_at"),
            ):
                summary[f"{table}_pruned"] = await _exec_count(
                    db, f"DELETE FROM {table} WHERE {col} < :c", {"c": cutoffs[key]}
                )
                await db.commit()

    total = sum(v for v in summary.values() if isinstance(v, int))
    if total:
        logger.info(f"[retention] deduped/pruned {total} rows: {summary}")

    # ── 3. VACUUM (outside any transaction, autocommit) ─────────────────────────
    # Plain VACUUM keeps bloat in check (safe, non-locking). vacuum_full=True does
    # a one-time disk reclaim (locks each table briefly) — this is what actually
    # returns space to the OS and shrinks the Railway Volume.
    if getattr(settings, "retention_vacuum_enabled", True) or vacuum_full:
        targets = vacuum_targets(summary, vacuum_full)
        if targets:
            cap = float(getattr(settings, "vacuum_full_max_table_mb",
                                VACUUM_FULL_MAX_TABLE_MB))
            n, errors, skipped = await _run_vacuum(
                targets, full=vacuum_full, max_table_mb=cap
            )
            summary["tables_vacuumed"] = n
            summary["tables_attempted"] = len(targets)
            if errors:
                # Surface failures to the caller/UI instead of silently reporting 0.
                summary["vacuum_errors"] = errors
            if skipped:
                # A skip is a decision, not a failure — say so explicitly so the
                # screen does not read it as "nothing happened".
                summary["vacuum_skipped_too_large"] = skipped
            logger.info(
                f"[retention] VACUUM{' FULL' if vacuum_full else ''}: "
                f"{n}/{len(targets)} succeeded"
                + (f", {len(errors)} failed" if errors else "")
                + (f", {len(skipped)} skipped (too large)" if skipped else "")
            )
    return summary
