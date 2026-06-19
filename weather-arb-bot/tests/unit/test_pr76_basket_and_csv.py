"""Tests for PR #76 features.

Three components:
1. Basket strategy — multi-bucket NO play detection (Warsaw 16/6 archetype)
2. Basket EV maths — net EV per share calculation
3. CollectorMiss model — missing weather source tracking
4. Basket ID tagging — virtual positions tagged with basket_id
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
from app.models.collector_miss import CollectorMiss
import app.intraday.detector as idet


# SQLite shims ────────────────────────────────────────────────────────────────
@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_as_int(type_, compiler, **kw):
    return "INTEGER"


# ── Basket EV maths (pure) ───────────────────────────────────────────────────

def _basket_ev(n: int, avg_entry: float) -> float:
    return (n - 1) / n - avg_entry


def test_basket_ev_positive_3_legs():
    """3 legs at avg 60¢: EV = 2/3 - 0.60 = +0.067 per share."""
    assert _basket_ev(3, 0.60) == pytest.approx(0.0667, abs=1e-3)


def test_basket_ev_positive_4_legs():
    """Warsaw 16/6 archetype: 4 legs at avg 55¢: EV = 3/4 - 0.55 = +0.20."""
    assert _basket_ev(4, 0.55) == pytest.approx(0.20, abs=1e-6)


def test_basket_ev_negative_when_expensive():
    """At 75¢ avg for 3 legs: EV = 2/3 - 0.75 < 0 — not a basket play."""
    assert _basket_ev(3, 0.75) < 0


def test_basket_min_buckets_threshold():
    """2 legs never qualify — must be >= BASKET_MIN_BUCKETS (3)."""
    assert idet.BASKET_MIN_BUCKETS == 3


# ── Basket detection DB tests ─────────────────────────────────────────────────

@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [
        m.__table__ for m in (
            City, Market, MarketOutcome, IntradayOpportunity, CollectorMiss
        )
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_market(db) -> tuple[City, Market]:
    city = City(
        name="Warsaw", primary_icao="EPWA", timezone="Europe/Warsaw", active=True,
        wunderground_url="https://www.wunderground.com/history/daily/EPWA",
    )
    db.add(city)
    await db.flush()
    market = Market(
        city_id=city.id, external_id="waw-16c", question="Highest temp in Warsaw?",
        event_date=date.today(), resolved=False,
    )
    db.add(market)
    await db.flush()
    return city, market


def _make_no_opp(outcome_id: int, entry: float, shares: int = 5) -> IntradayOpportunity:
    return IntradayOpportunity(
        outcome_id=outcome_id, side="NO", market_price=0.5,
        estimated_true_prob=0.4, edge=0.1, confidence_score=92, signals={},
        virtual_shares=shares,
        virtual_entry_price=entry,
        virtual_cost=round(shares * entry, 4),
        virtual_status="open",
    )


async def _seed_outcomes(db, market, n: int) -> list[MarketOutcome]:
    outcomes = []
    for i in range(n):
        o = MarketOutcome(
            market_id=market.id,
            bucket_label=f"{15 + i}°C",
            bucket_min=15 + i,
            bucket_max=15 + i,
        )
        db.add(o)
        outcomes.append(o)
    await db.flush()
    return outcomes


async def test_evaluate_basket_positive_ev(db):
    """4 qualifying NO buys at avg 55¢ → basket detected, basket_id assigned."""
    city, market = await _seed_market(db)
    outcomes = await _seed_outcomes(db, market, 4)

    opps = []
    for outcome, entry in zip(outcomes, [0.50, 0.55, 0.60, 0.55]):
        opp = _make_no_opp(outcome.id, entry)
        db.add(opp)
        opps.append(opp)
    await db.commit()
    for opp in opps:
        await db.refresh(opp)

    market_opps = [(opp, outcomes[i].bucket_label) for i, opp in enumerate(opps)]
    basket = await idet._evaluate_basket(db, market, city, market_opps)

    assert basket is not None
    assert basket["n_legs"] == 4
    assert basket["city_name"] == "Warsaw"
    assert basket["ev_per_share"] > 0
    # All legs should have been tagged with the basket_id
    for opp in opps:
        await db.refresh(opp)
        assert opp.basket_id == basket["basket_id"]
    assert basket["basket_id"].startswith("bkt_")


async def test_evaluate_basket_too_few_legs(db):
    """2 qualifying NO buys → below BASKET_MIN_BUCKETS, no basket."""
    city, market = await _seed_market(db)
    outcomes = await _seed_outcomes(db, market, 2)

    opps = []
    for outcome in outcomes:
        opp = _make_no_opp(outcome.id, 0.50)
        db.add(opp)
        opps.append(opp)
    await db.commit()

    market_opps = [(opp, outcomes[i].bucket_label) for i, opp in enumerate(opps)]
    basket = await idet._evaluate_basket(db, market, city, market_opps)
    assert basket is None


async def test_evaluate_basket_negative_ev(db):
    """3 legs at 80¢ avg → EV = 2/3 - 0.80 < 0, no basket."""
    city, market = await _seed_market(db)
    outcomes = await _seed_outcomes(db, market, 3)

    opps = []
    for outcome in outcomes:
        opp = _make_no_opp(outcome.id, 0.80)
        db.add(opp)
        opps.append(opp)
    await db.commit()

    market_opps = [(opp, outcomes[i].bucket_label) for i, opp in enumerate(opps)]
    basket = await idet._evaluate_basket(db, market, city, market_opps)
    assert basket is None


async def test_evaluate_basket_yes_legs_excluded(db):
    """YES legs don't count toward basket — only open NO buys qualify."""
    city, market = await _seed_market(db)
    outcomes = await _seed_outcomes(db, market, 4)

    opps = []
    for i, outcome in enumerate(outcomes):
        # 1 YES + 3 NO → only 3 valid legs but still might form a basket
        side = "YES" if i == 0 else "NO"
        opp = IntradayOpportunity(
            outcome_id=outcome.id, side=side, market_price=0.5,
            estimated_true_prob=0.6 if side == "YES" else 0.4,
            edge=0.1, confidence_score=92, signals={},
            virtual_shares=5, virtual_entry_price=0.55,
            virtual_cost=2.75, virtual_status="open",
        )
        db.add(opp)
        opps.append(opp)
    await db.commit()

    market_opps = [(opp, outcomes[i].bucket_label) for i, opp in enumerate(opps)]
    basket = await idet._evaluate_basket(db, market, city, market_opps)
    # 3 NO legs at 0.55 → EV = 2/3 - 0.55 = +0.117 → should be a basket
    assert basket is not None
    assert basket["n_legs"] == 3


async def test_evaluate_basket_only_open_positions(db):
    """Settled (win/loss) positions don't count as active basket legs."""
    city, market = await _seed_market(db)
    outcomes = await _seed_outcomes(db, market, 4)

    opps = []
    for i, outcome in enumerate(outcomes):
        opp = _make_no_opp(outcome.id, 0.55)
        if i == 0:
            opp.virtual_status = "win"  # settled — should not count
        db.add(opp)
        opps.append(opp)
    await db.commit()

    # Only pass the open ones as market_opps (simulating what detect_intraday does)
    market_opps = [(opps[i], outcomes[i].bucket_label) for i in range(1, 4)]
    basket = await idet._evaluate_basket(db, market, city, market_opps)
    # 3 open NO legs at 0.55 → EV > 0 → basket
    assert basket is not None
    assert basket["n_legs"] == 3


# ── Basket summary fields ─────────────────────────────────────────────────────

async def test_basket_summary_fields(db):
    """Verify all expected summary fields are present and correct."""
    city, market = await _seed_market(db)
    outcomes = await _seed_outcomes(db, market, 3)

    entries = [0.50, 0.55, 0.60]
    opps = []
    for outcome, entry in zip(outcomes, entries):
        opp = _make_no_opp(outcome.id, entry, shares=5)
        db.add(opp)
        opps.append(opp)
    await db.commit()

    market_opps = [(opp, outcomes[i].bucket_label) for i, opp in enumerate(opps)]
    basket = await idet._evaluate_basket(db, market, city, market_opps)

    assert basket is not None
    assert "basket_id" in basket
    assert "n_legs" in basket
    assert "buckets" in basket
    assert "avg_entry_cost" in basket
    assert "total_cost" in basket
    assert "expected_payout" in basket
    assert "net_pnl_if_one_wins" in basket
    assert "ev_per_share" in basket
    assert "legs" in basket

    # Verify maths
    avg = sum(entries) / 3
    assert basket["avg_entry_cost"] == pytest.approx(avg, abs=1e-4)
    expected_payout = 2 * 5 * 1.0  # (N-1) * shares_per_leg
    assert basket["expected_payout"] == pytest.approx(expected_payout, abs=1e-4)
    total_cost = sum(5 * e for e in entries)
    assert basket["total_cost"] == pytest.approx(total_cost, abs=1e-2)
    assert basket["net_pnl_if_one_wins"] == pytest.approx(expected_payout - total_cost, abs=1e-2)


# ── CollectorMiss model ───────────────────────────────────────────────────────

async def test_collector_miss_insert(db):
    """CollectorMiss records can be created and retrieved."""
    city = City(
        name="Tokyo", primary_icao="RJTT", timezone="Asia/Tokyo", active=True,
        wunderground_url="https://www.wunderground.com/history/daily/RJTT",
    )
    db.add(city)
    await db.flush()

    miss = CollectorMiss(
        city_id=city.id,
        event_date=date.today(),
        source="ECMWF",
        miss_reason="no_data",
    )
    db.add(miss)
    await db.commit()
    await db.refresh(miss)

    assert miss.id is not None
    assert miss.city_id == city.id
    assert miss.source == "ECMWF"
    assert miss.miss_reason == "no_data"


async def test_collector_miss_unique_constraint(db):
    """Duplicate (city, date, source, reason) inserts must not create duplicates."""
    from sqlalchemy import select as sa_select
    city = City(
        name="Mexico City", primary_icao="MMMX", timezone="America/Mexico_City",
        active=True,
        wunderground_url="https://www.wunderground.com/history/daily/MMMX",
    )
    db.add(city)
    await db.flush()

    today = date.today()
    miss1 = CollectorMiss(city_id=city.id, event_date=today, source="ECMWF", miss_reason="no_data")
    db.add(miss1)
    await db.commit()

    # Second insert with identical key should fail at DB level (unique constraint)
    miss2 = CollectorMiss(city_id=city.id, event_date=today, source="ECMWF", miss_reason="no_data")
    db.add(miss2)
    with pytest.raises(Exception):
        await db.commit()
