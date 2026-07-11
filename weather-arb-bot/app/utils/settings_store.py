"""Persistence layer for runtime-adjustable settings.

The admin Settings tab mutates the in-memory `settings` object so changes take
effect immediately — but before this module existed they were lost on every
restart/deploy, silently reverting thresholds to config defaults. Now:

  - save_setting_override() upserts the value into the app_settings table.
  - load_setting_overrides() is called once at startup (before the scheduler
    starts) and re-applies every stored override onto the settings object.

Only keys in PERSISTABLE_KEYS are accepted, so a corrupted row can never
overwrite arbitrary attributes (API keys, DB URLs, etc.).
"""
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.app_setting import AppSetting

logger = logging.getLogger(__name__)

# Runtime-tunable keys only. Everything else stays env/config-driven.
PERSISTABLE_KEYS: frozenset = frozenset({
    "min_confidence_for_alert",
    "min_edge_for_alert",
    "max_days_ahead_for_alert",
    "min_confidence_alert_near",
    "min_confidence_alert_far",
    "min_confidence_buy_near",
    "min_confidence_buy_far",
    "min_confidence_beta_alert",
    "min_confidence_beta_buy",
})


async def save_setting_override(db: AsyncSession, key: str, value: Any) -> bool:
    """Upsert one override. Returns False (and does nothing) for unknown keys."""
    if key not in PERSISTABLE_KEYS:
        logger.warning(f"settings_store: refusing to persist non-whitelisted key {key!r}")
        return False
    existing = await db.get(AppSetting, key)
    if existing is None:
        db.add(AppSetting(key=key, value=value))
    else:
        existing.value = value
    await db.commit()
    return True


async def load_setting_overrides(db: AsyncSession) -> int:
    """Apply all persisted overrides onto the in-memory settings object.

    Returns the number of overrides applied. Never raises — a failure to load
    must not prevent the app from starting with config defaults.
    """
    applied = 0
    try:
        result = await db.execute(select(AppSetting))
        for row in result.scalars().all():
            if row.key not in PERSISTABLE_KEYS:
                logger.warning(f"settings_store: skipping unknown persisted key {row.key!r}")
                continue
            try:
                setattr(settings, row.key, row.value)
                applied += 1
            except Exception as e:
                logger.error(f"settings_store: failed to apply {row.key!r}: {e}")
        if applied:
            logger.info(f"settings_store: applied {applied} persisted setting override(s)")
    except Exception as e:
        # Table may not exist yet (migration not run) — start with defaults.
        logger.warning(f"settings_store: could not load overrides (using defaults): {e}")
    return applied
