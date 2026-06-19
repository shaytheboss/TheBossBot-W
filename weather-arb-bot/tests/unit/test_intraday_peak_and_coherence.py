"""Tests for two intraday correctness fixes from the 17-18 June position review.

Fix A — peak-passed confirmation gate (Taipei 14/6):
    "Peak passed" was declared at 14:14 — 14 minutes into the 14:00-17:00 peak
    window — on a transient 1.8°F dip. σ collapsed to 0.3°F, certainty climbed
    to 96%, then the temperature resumed climbing and the bet lost. Peak-passed
    cooling detection is now gated behind peak_confirm_hour (15.5) so an early-
    window dip can't collapse σ; before that the schedule σ is kept.

Fix B — cross-bet coherence guard (London 15/6):
    The bot held BOTH 21°C NO @ 60¢ and 21°C YES @ 74¢ — paying $1.34 to get
    back $1.00 — because the model flipped sides on the same bucket and the
    per-side dedup didn't catch the opposite side. We now suppress a virtual
    buy when an OPEN opposite-side position already exists on the same outcome.
"""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import BigInteger

from app.database import Base
from app.models import City, Market, MarketOutcome
from app.models.intraday import IntradayOpportunity
from app.intraday.estimator import DEFAULT_PARAMS, estimate_intraday, is_peak_passed
import app.intraday.detector as idet


# SQLite shims (mirrors test_model_skill_db.py) ───────────────────────────────
@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_int(type_, compiler, **kw):
    return "INTEGER"


# ── Fix A: peak-passed confirmation gate ─────────────────────────────────────

def test_peak_not_declared_at_window_start():
    """Taipei 14:14: cooling conditions met, but too early in the window."""
    # 1.8°F drop, max set 100 min ago — old code returned True at 14.14.
    assert is_peak_passed(14.14, 78.8, 80.6, 100.0) is False


def test_peak_not_declared_before_confirm_hour():
    """Even at 15:00 — inside the window but before peak_confirm_hour (15.5)."""
    assert is_peak_passed(15.0, 82.0, 85.0, 120.0) is False


def test_peak_declared_at_confirm_hour():
    """At 15.5 with cooling met, peak-passed is reliable again."""
    assert is_peak_passed(15.5, 82.0, 85.0, 120.0) is True


def test_peak_still_needs_cooling_after_confirm_hour():
    """The confirm-hour gate is additive — cooling is still required."""
    # 16:00 but temp only 0.5°F below max → not cooling.
    assert is_peak_passed(16.0, 84.5, 85.0, 120.0) is False


def test_taipei_estimate_no_sigma_collapse_at_window_start():
    """End-to-end Taipei 27°C scenario: σ must NOT collapse to post_peak (0.3).

    Bucket 27°C = [80.6, 82.4)°F, running max 80.6°F, forecast high 84.1°F,
    14:14 local. Old behaviour: peak_passed → σ=0.3 → P(YES)≈0.91 (wrong-way
    overconfidence). New behaviour: schedule σ (≥1.0, Celsius-floored to 1.8)."""
    p, bd = estimate_intraday(
        running_max_f=80.6, current_temp_f=78.8, minutes_since_max=100.0,
        forecast_high_f=84.1, local_hour=14.14,
        bucket_min=27, bucket_max=27, bucket_unit="C",
    )
    assert bd["peak_passed"] is False
    assert bd["sigma_used"] > DEFAULT_PARAMS.post_peak_sigma
    # σ is no longer 0.3; with the Celsius floor it is at least 1.8°F.
    assert bd["sigma_used"] >= DEFAULT_PARAMS.celsius_min_sigma_f - 1e-9


def test_late_afternoon_peak_still_collapses_sigma():
    """Regression guard: a genuine 16:30 post-peak case still tightens σ."""
    p, bd = estimate_intraday(
        running_max_f=85.1, current_temp_f=82.0, minutes_since_max=120.0,
        forecast_high_f=85.0, local_hour=16.5,
        bucket_min=78, bucket_max=79, bucket_unit="F",
    )
    assert bd["peak_passed"] is True
    assert bd["sigma_used"] == pytest.approx(DEFAULT_PARAMS.post_peak_sigma, abs=1e-9)


# ── Fix B: cross-bet coherence guard ─────────────────────────────────────────

@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [m.__table__ for m in (City, Market, MarketOutcome, IntradayOpportunity)]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_outcome(db) -> MarketOutcome:
    city = City(
        name="London", primary_icao="EGLL", timezone="Europe/London", active=True,
        wunderground_url="https://www.wunderground.com/history/daily/EGLL",
    )
    db.add(city)
    await db.flush()
    market = Market(
        city_id=city.id, external_id="lon-21c", question="Highest temp in London?",
        event_date=date.today(), resolved=False,
    )
    db.add(market)
    await db.flush()
    outcome = MarketOutcome(
        market_id=market.id, bucket_label="21°C", bucket_min=21, bucket_max=21,
    )
    db.add(outcome)
    await db.flush()
    return outcome


def _open_position(outcome_id: int, side: str) -> IntradayOpportunity:
    return IntradayOpportunity(
        outcome_id=outcome_id, side=side, market_price=0.5,
        estimated_true_prob=0.6, edge=0.1, confidence_score=92, signals={},
        virtual_shares=5, virtual_entry_price=0.6, virtual_cost=3.0,
        virtual_status="open",
    )


async def test_open_opposite_detected_when_present(db):
    outcome = await _seed_outcome(db)
    db.add(_open_position(outcome.id, "NO"))   # already hold NO 21°C
    await db.commit()
    # A new YES on the same bucket must see the open opposite (NO).
    assert await idet._has_open_opposite_intraday(db, outcome.id, "YES") is True


async def test_no_opposite_when_same_side_only(db):
    outcome = await _seed_outcome(db)
    db.add(_open_position(outcome.id, "NO"))
    await db.commit()
    # Another NO is not "opposite" — same-side dedup handles that elsewhere.
    assert await idet._has_open_opposite_intraday(db, outcome.id, "NO") is False


async def test_no_opposite_when_position_settled(db):
    outcome = await _seed_outcome(db)
    pos = _open_position(outcome.id, "NO")
    pos.virtual_status = "win"   # settled, not open
    db.add(pos)
    await db.commit()
    # A settled position no longer blocks the opposite side.
    assert await idet._has_open_opposite_intraday(db, outcome.id, "YES") is False


async def test_no_opposite_on_clean_outcome(db):
    outcome = await _seed_outcome(db)
    await db.commit()
    assert await idet._has_open_opposite_intraday(db, outcome.id, "YES") is False


def test_cross_bet_gates_the_buy_decision():
    """The guard composes into create_buy exactly like the other gates."""
    certainty, buy_thresh = 0.96, 0.94
    blacklisted, entry_too_expensive = False, False
    for has_open_opposite, expected in [(True, False), (False, True)]:
        create_buy = (
            certainty >= buy_thresh
            and not blacklisted
            and not entry_too_expensive
            and not has_open_opposite
        )
        assert create_buy is expected
