"""Tests for _load_city_skill lead-time aggregation.

Root cause (beta calibration starvation): _load_city_skill previously
queried only the exact days_ahead slot. With one week of history split
across 4 lead-time buckets × 7 models × 48 cities, every cell had < 5
samples (MIN_SAMPLES), so _beta_source_weight / _beta_source_bias /
_beta_source_sigma all received None and ran as neutral alpha clones.
Beta's per-city intelligence was never activated.

Fix: aggregate across all lead-times when the exact slot is thin, producing
a _SkillSnapshot that activates corrections as soon as MIN_SAMPLES total
resolved markets exist for (city, model) — regardless of lead-time split.
"""
import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import BigInteger

from app.database import Base
from app.models import City, ModelSkill
from app.analyzers.model_skill import MIN_SAMPLES, skill_weight
from app.analyzers.beta_opportunity_detector import _load_city_skill, _SkillSnapshot


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_int(type_, compiler, **kw):
    return "INTEGER"


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [City.__table__, ModelSkill.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def city(db):
    c = City(
        name="TestCity", primary_icao="KTST", timezone="UTC",
        wunderground_url="https://example.com", active=True,
    )
    db.add(c)
    await db.flush()
    return c


async def _row(db, city_id, source, days_ahead, samples, hits, mae_f=2.0, bias_f=1.0):
    r = ModelSkill(
        city_id=city_id, source=source, days_ahead=days_ahead,
        samples=samples, hits=hits,
        hit_rate=round(hits / samples, 4) if samples else None,
        mae_f=mae_f, bias_f=bias_f,
        weight=skill_weight(hits, samples),
    )
    db.add(r)
    await db.flush()
    return r


# ── 1. Exact lead-time preferred when sufficient ─────────────────────────────

async def test_exact_lead_time_used_when_sufficient(db, city):
    """Exact days_ahead row with >= MIN_SAMPLES is returned as-is (not as _SkillSnapshot)."""
    await _row(db, city.id, "gfs", days_ahead=1, samples=6, hits=5)
    await _row(db, city.id, "gfs", days_ahead=2, samples=6, hits=1)  # worse — should be ignored
    await db.commit()

    result = await _load_city_skill(db, city.id, days_ahead=1)
    gfs = result["gfs"]
    assert not isinstance(gfs, _SkillSnapshot)
    assert gfs.samples == 6
    assert gfs.hits == 5


# ── 2. Aggregation fires when exact slot is thin ─────────────────────────────

async def test_aggregates_when_exact_lead_is_thin(db, city):
    """2 samples at days_ahead=1 (< MIN_SAMPLES=5), 4 at days_ahead=0 → combined=6 → snapshot."""
    await _row(db, city.id, "gfs", days_ahead=1, samples=2, hits=2, mae_f=1.0, bias_f=0.5)
    await _row(db, city.id, "gfs", days_ahead=0, samples=4, hits=3, mae_f=2.0, bias_f=1.0)
    await db.commit()

    result = await _load_city_skill(db, city.id, days_ahead=1)
    gfs = result["gfs"]
    assert isinstance(gfs, _SkillSnapshot)
    assert gfs.samples == 6
    assert gfs.hits == 5
    # MAE: weighted avg (1.0×2 + 2.0×4) / 6 = 10/6 ≈ 1.67
    assert gfs.mae_f == pytest.approx(1.67, abs=0.01)
    # bias: weighted avg (0.5×2 + 1.0×4) / 6 = 5/6 ≈ 0.83
    assert gfs.bias_f == pytest.approx(0.83, abs=0.01)
    assert gfs.weight == skill_weight(5, 6)


# ── 3. Source absent at requested lead but present elsewhere ─────────────────

async def test_source_absent_at_requested_lead_aggregated(db, city):
    """No row at days_ahead=2 but da=0 + da=1 combined >= MIN_SAMPLES → snapshot."""
    await _row(db, city.id, "meteosource", days_ahead=0, samples=3, hits=3, mae_f=1.5, bias_f=0.5)
    await _row(db, city.id, "meteosource", days_ahead=1, samples=3, hits=2, mae_f=2.5, bias_f=1.5)
    await db.commit()

    result = await _load_city_skill(db, city.id, days_ahead=2)
    snap = result.get("meteosource")
    assert snap is not None
    assert isinstance(snap, _SkillSnapshot)
    assert snap.samples == 6
    assert snap.hits == 5
    # MAE: (1.5×3 + 2.5×3) / 6 = 12/6 = 2.0
    assert snap.mae_f == pytest.approx(2.0, abs=0.01)


# ── 4. Still too thin even combined → neutral ────────────────────────────────

async def test_too_thin_combined_stays_neutral(db, city):
    """Combined samples < MIN_SAMPLES: the thin exact row is returned (not a snapshot)
    so the breakdown dict can log it, but the estimator treats it as neutral."""
    await _row(db, city.id, "ecmwf", days_ahead=1, samples=1, hits=1)
    await _row(db, city.id, "ecmwf", days_ahead=2, samples=1, hits=0)
    await db.commit()

    result = await _load_city_skill(db, city.id, days_ahead=1)
    ecmwf = result.get("ecmwf")
    # Either not in result or present as the thin exact row (never a snapshot)
    assert not isinstance(ecmwf, _SkillSnapshot)
    if ecmwf is not None:
        assert ecmwf.samples < MIN_SAMPLES


# ── 5. Multiple sources with mixed routing ───────────────────────────────────

async def test_multiple_sources_correct_routing(db, city):
    """GFS has enough at exact lead → exact row.
    ECMWF thin at exact lead but enough combined → snapshot.
    ICON too thin anywhere → thin exact row (not snapshot)."""
    await _row(db, city.id, "gfs",    days_ahead=1, samples=MIN_SAMPLES + 1, hits=5)
    await _row(db, city.id, "ecmwf",  days_ahead=1, samples=2, hits=2)
    await _row(db, city.id, "ecmwf",  days_ahead=0, samples=4, hits=3)
    await _row(db, city.id, "icon",   days_ahead=1, samples=1, hits=1)
    await db.commit()

    result = await _load_city_skill(db, city.id, days_ahead=1)

    # GFS: exact row, not snapshot
    assert not isinstance(result["gfs"], _SkillSnapshot)
    assert result["gfs"].samples == MIN_SAMPLES + 1

    # ECMWF: aggregated snapshot
    assert isinstance(result["ecmwf"], _SkillSnapshot)
    assert result["ecmwf"].samples == 6

    # ICON: too thin combined (1 sample) → thin exact row, not snapshot
    icon = result.get("icon")
    assert not isinstance(icon, _SkillSnapshot)
    if icon is not None:
        assert icon.samples < MIN_SAMPLES


# ── 6. No rows at all → empty dict ──────────────────────────────────────────

async def test_no_skill_data_returns_empty(db, city):
    """No model_skill rows for this city → empty dict (beta runs neutral)."""
    await db.commit()
    result = await _load_city_skill(db, city.id, days_ahead=0)
    assert result == {}
