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
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.config import settings
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


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


async def _exec_count(db, sql: str, params: dict | None = None) -> int:
    """Run a DELETE and return affected row count; never raises."""
    try:
        result = await db.execute(text(sql), params or {})
        return int(result.rowcount or 0)
    except Exception as e:
        logger.error(f"[retention] statement failed: {e}", exc_info=True)
        await db.rollback()
        return 0


async def job_prune_old_data() -> dict:
    """Daily maintenance. Two independent, separately-gated steps:

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
    if not dedup_on and not prune_on:
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

    total = sum(summary.values())
    if total:
        logger.info(f"[retention] deduped/pruned {total} rows: {summary}")
    return summary
