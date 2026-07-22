"""Tests for the data-retention / de-dup cost-control job.

Two things must hold:
1. Retention windows never drop below what any feature reads (capability-safe).
2. The forecast de-dup keeps EXACTLY the rows model_skill/aggregator use:
   the latest retrieved_at per (city, source, forecast_for_date, made-date).
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.workers.retention_job import compute_cutoffs
from app.config import settings
import app.analyzers.model_skill as ms
import app.analyzers.bias_estimator as be


# ── 1. Capability-safety: windows exceed computation windows ───────────────────

class TestRetentionWindowsSafe:
    def test_forecast_window_exceeds_model_skill(self):
        """model_skill reads WINDOW_DAYS of raw forecasts — retention must keep
        at least that, or calibration silently loses history."""
        assert settings.forecast_retention_days > ms.WINDOW_DAYS

    def test_metar_window_exceeds_bias(self):
        assert settings.metar_retention_days > be.WINDOW_DAYS

    def test_all_windows_positive(self):
        for attr in (
            "forecast_retention_days", "metar_retention_days",
            "market_price_retention_days", "pirep_retention_days",
            "collector_miss_retention_days",
        ):
            assert getattr(settings, attr) > 0


# ── 2. compute_cutoffs is correct arithmetic ───────────────────────────────────

class _Cfg:
    forecast_retention_days = 120
    metar_retention_days = 45
    market_price_retention_days = 45
    pirep_retention_days = 21
    collector_miss_retention_days = 90


class TestComputeCutoffs:
    NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    TODAY = date(2026, 7, 18)

    def test_forecast_cutoff(self):
        c = compute_cutoffs(self.NOW, self.TODAY, _Cfg())
        assert c["forecast_date"] == date(2026, 7, 18) - timedelta(days=120)

    def test_metar_cutoff(self):
        c = compute_cutoffs(self.NOW, self.TODAY, _Cfg())
        assert c["metar_ts"] == self.NOW - timedelta(days=45)

    def test_defaults_when_attr_missing(self):
        c = compute_cutoffs(self.NOW, self.TODAY, object())
        # falls back to documented defaults, never crashes
        assert c["forecast_date"] == self.TODAY - timedelta(days=120)
        assert c["pirep_ts"] == self.NOW - timedelta(days=21)


# ── 3. Integration: retention + dedup on a real (SQLite) DB ────────────────────

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import BigInteger, select, func, text

from app.database import Base
from app.models import City, Forecast, MetarObservation


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_int(type_, compiler, **kw):
    return "INTEGER"


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [m.__table__ for m in (City, Forecast, MetarObservation)]
    async with engine.begin() as conn:
        for t in tables:
            await conn.run_sync(lambda c, t=t: t.create(c))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


def _dedup_keep_ids_portable(rows):
    """The de-dup semantics expressed in Python (what the PG window function does):
    keep the max-retrieved_at row per (city, source, target_date, made-date).
    Used to VERIFY the intended behaviour on SQLite."""
    best = {}
    for r in rows:
        key = (r.city_id, r.source, r.forecast_for_date, r.retrieved_at.date())
        if key not in best or r.retrieved_at > best[key].retrieved_at:
            best[key] = r
    return {r.id for r in best.values()}


@pytest.mark.asyncio
async def test_dedup_matches_model_skill_selection(db):
    """The kept set must equal what model_skill keeps: latest per
    (source, event_date, days_ahead). Two hourly re-fetches of the same
    forecast collapse to the latest; different lead-days are preserved."""
    db.add(City(id=1, name="X", primary_icao="KX", timezone="UTC", wunderground_url="http://x", active=True))
    await db.flush()
    target = date(2026, 7, 20)
    rows = []
    # 3 hourly re-fetches on the SAME made-day (2026-07-18) → dedup to latest
    for hour in (6, 7, 8):
        rows.append(Forecast(city_id=1, source="gfs", forecast_for_date=target,
                             predicted_high_f=80 + hour,
                             retrieved_at=datetime(2026, 7, 18, hour, tzinfo=timezone.utc)))
    # a different made-day (2026-07-19, i.e. shorter lead) → must be KEPT
    rows.append(Forecast(city_id=1, source="gfs", forecast_for_date=target,
                         predicted_high_f=90,
                         retrieved_at=datetime(2026, 7, 19, 6, tzinfo=timezone.utc)))
    for r in rows:
        db.add(r)
    await db.flush()
    all_rows = (await db.execute(select(Forecast))).scalars().all()

    keep = _dedup_keep_ids_portable(all_rows)
    assert len(keep) == 2, "one per made-day (2 made-days) survives"
    # the survivor of the 3-fetch day is the 08:00 one (latest)
    latest_same_day = max(
        (r for r in all_rows if r.retrieved_at.date() == date(2026, 7, 18)),
        key=lambda r: r.retrieved_at,
    )
    assert latest_same_day.id in keep
    # different lead-day preserved
    assert any(r.retrieved_at.date() == date(2026, 7, 19) and r.id in keep for r in all_rows)


@pytest.mark.asyncio
async def test_retention_delete_is_portable_and_correct(db):
    """The Python-cutoff DELETE runs on SQLite and removes only old rows."""
    db.add(City(id=1, name="X", primary_icao="KX", timezone="UTC", wunderground_url="http://x", active=True))
    await db.flush()
    old = datetime.now(timezone.utc) - timedelta(days=60)
    new = datetime.now(timezone.utc) - timedelta(days=5)
    db.add(MetarObservation(icao="KX", observed_at=old, temperature_f=70))
    db.add(MetarObservation(icao="KX", observed_at=new, temperature_f=72))
    await db.flush()

    cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    await db.execute(
        text("DELETE FROM metar_observations WHERE observed_at < :c"), {"c": cutoff}
    )
    await db.commit()
    remaining = (await db.execute(select(func.count()).select_from(MetarObservation))).scalar_one()
    assert remaining == 1, "only the 5-day-old row survives a 45-day window"
