"""The duplicate-markets view: cities with more than one market on one day.

New York and London each carried a second, never-priced 7-bucket market every
day, and the shadow study scored it with the city's own forecast. This view
names every such market so it can be identified from its Polymarket event.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.api.admin import admin_duplicate_markets
from app.models.market import Market, MarketOutcome, MarketPrice
from tests.fixtures import cities as city_fixtures

DAY = date.today() + timedelta(days=1)


async def _market(db, mid, city_id, slug, question, buckets, priced, day=DAY):
    db.add(Market(id=mid, city_id=city_id, external_id=slug, question=question, event_date=day))
    for i in range(buckets):
        oid = mid * 100 + i
        db.add(MarketOutcome(id=oid, market_id=mid, bucket_label=f"b{i}", bucket_min=i,
                             bucket_max=i, bucket_unit="F", token_id=f"t{oid}" if priced else None))
        if priced:
            db.add(MarketPrice(outcome_id=oid, timestamp=datetime.now(timezone.utc),
                               yes_price=0.5, no_price=0.5))


@pytest.fixture
async def db(sqlite_db):
    for name in ("New York", "Austin"):
        sqlite_db.add(city_fixtures.get(name).as_row())
    await sqlite_db.commit()
    return sqlite_db


@pytest.mark.asyncio
async def test_a_city_with_two_markets_on_one_day_is_listed_with_names_and_links(db):
    ny, austin = city_fixtures.get("New York").id, city_fixtures.get("Austin").id
    await _market(db, 1, ny, "highest-temperature-in-nyc-on-x", "Highest temperature in NYC?", 11, True)
    await _market(db, 2, ny, "some-other-event", "Some other question?", 7, False)
    await _market(db, 3, austin, "highest-temperature-in-austin-on-x", "Austin?", 11, True)
    await db.commit()

    out = await admin_duplicate_markets("t", db, days=10)
    assert out["city_days_with_several_markets"] == 1, "Austin has one market — not listed"
    g = out["groups"][0]
    assert g["city"] == "New York" and g["event_date"] == DAY.isoformat()
    by_id = {m["market_id"]: m for m in g["markets"]}
    assert by_id[1]["buckets"] == 11 and by_id[1]["last_price_at"] is not None
    assert by_id[2]["buckets"] == 7 and by_id[2]["last_price_at"] is None
    assert by_id[2]["buckets_with_token"] == 0
    assert by_id[2]["question"] == "Some other question?"
    assert by_id[2]["link"] == "https://polymarket.com/event/some-other-event"


@pytest.mark.asyncio
async def test_markets_on_different_days_are_not_duplicates(db):
    ny = city_fixtures.get("New York").id
    await _market(db, 1, ny, "a", "A?", 3, True, day=DAY)
    await _market(db, 2, ny, "b", "B?", 3, True, day=DAY + timedelta(days=1))
    await db.commit()
    assert (await admin_duplicate_markets("t", db, days=10))["groups"] == []


@pytest.mark.asyncio
async def test_old_days_are_outside_the_window(db):
    ny = city_fixtures.get("New York").id
    old = date.today() - timedelta(days=30)
    await _market(db, 1, ny, "a", "A?", 3, True, day=old)
    await _market(db, 2, ny, "b", "B?", 3, False, day=old)
    await db.commit()
    assert (await admin_duplicate_markets("t", db, days=10))["groups"] == []
    assert len((await admin_duplicate_markets("t", db, days=40))["groups"]) == 1
