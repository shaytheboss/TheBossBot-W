"""Last year's markets must not come back as this year's.

Polymarket's 2025 slugs had no year ("highest-temperature-in-nyc-on-
september-26"); 2026's end in "-2026". The year-less slug was read as this
year, so New York and London carried a second market every day — closed a
year ago, never priced, resolved with 2025's winner — and the shadow study and
model_skill scored 2026 forecasts against it. The admin "Duplicate markets"
view showed 26 such city-days in 10 days.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

import app.shadow.models  # noqa: F401  — registers the tables
from app.models.market import Market, MarketOutcome
from app.shadow.models import ShadowSnapshot
from app.workers.jobs import _ingest_event, _is_stale_event
from tests.fixtures import cities as city_fixtures
from tests.mocks import polymarket_payloads as pm
from tests.mocks import sqlite_compat

DAY = date.today() + timedelta(days=1)
MONTH = DAY.strftime("%B").lower()


def _event(slug, end: date | None, closed=False):
    e = pm.event(slug=slug, title=f"Highest temperature in NYC on {DAY:%B} {DAY.day}?",
                 closed=closed)
    if end is not None:
        e["endDate"] = datetime.combine(end, time(12), tzinfo=timezone.utc).isoformat()
    return e


YEARLESS = f"highest-temperature-in-nyc-on-{MONTH}-{DAY.day}"
WITH_YEAR = f"{YEARLESS}-{DAY.year}"
LAST_YEAR = DAY.replace(year=DAY.year - 1)


class TestTheCheck:
    def test_last_years_event_is_stale(self):
        assert _is_stale_event(_event(YEARLESS, LAST_YEAR), DAY)

    def test_this_years_event_is_not(self):
        assert not _is_stale_event(_event(WITH_YEAR, DAY), DAY)

    def test_an_end_the_morning_after_is_fine(self):
        assert not _is_stale_event(_event(WITH_YEAR, DAY + timedelta(days=1)), DAY)

    def test_a_closed_event_is_stale(self):
        """Ingest only takes today or later, so an already-closed event is old."""
        assert _is_stale_event(_event(WITH_YEAR, DAY, closed=True), DAY)

    def test_no_end_date_changes_nothing(self):
        assert not _is_stale_event(_event(YEARLESS, None), DAY)


class TestIngest:
    @pytest.fixture
    async def db(self, sqlite_db):
        sqlite_db.add(city_fixtures.get("New York").as_row())
        await sqlite_db.commit()
        return sqlite_db

    async def _ingest(self, db, event):
        city = city_fixtures.get("New York")
        from app.models.city import City
        row = (await db.execute(select(City).where(City.id == city.id))).scalar_one()
        return await _ingest_event(event, row, db)

    @pytest.mark.asyncio
    async def test_last_years_market_is_not_stored(self, db):
        assert await self._ingest(db, _event(YEARLESS, LAST_YEAR)) == (0, 0)
        assert (await db.execute(select(Market))).first() is None

    @pytest.mark.asyncio
    async def test_this_years_market_is_stored_on_its_day(self, db):
        created, _ = await self._ingest(db, _event(WITH_YEAR, DAY))
        m = (await db.execute(select(Market))).scalar_one()
        assert created > 0 and m.event_date == DAY and m.external_id == WITH_YEAR

    @pytest.mark.asyncio
    async def test_both_seen_only_this_years_is_kept(self, db):
        """What discovery did every day for New York and London."""
        await self._ingest(db, _event(YEARLESS, LAST_YEAR))
        await self._ingest(db, _event(WITH_YEAR, DAY))
        slugs = [m.external_id for m in (await db.execute(select(Market))).scalars()]
        assert slugs == [WITH_YEAR]


class TestShadowExport:
    @pytest.mark.asyncio
    async def test_snapshots_of_a_moved_market_are_left_out_not_deleted(self, monkeypatch):
        from app.database import Base
        import app.models  # noqa: F401
        from app.api.admin import admin_shadow_csv

        engine = sqlite_compat.make_engine()
        await sqlite_compat.create_schema(engine, Base.metadata)
        maker = sqlite_compat.make_sessionmaker(engine)
        monkeypatch.setattr("app.utils.csv_stream.AsyncSessionLocal", maker)
        ny = city_fixtures.get("New York")
        now = datetime.now(timezone.utc)
        async with maker() as db:
            db.add(ny.as_row())
            # market 1: real; market 2: last year's, date already corrected
            db.add(Market(id=1, city_id=ny.id, external_id=WITH_YEAR, question="q", event_date=DAY))
            db.add(Market(id=2, city_id=ny.id, external_id=YEARLESS, question="q", event_date=LAST_YEAR))
            for oid, mid in ((10, 1), (20, 2)):
                db.add(MarketOutcome(id=oid, market_id=mid, bucket_label=f"b{oid}", bucket_unit="F"))
                db.add(ShadowSnapshot(outcome_id=oid, taken_at=now, market_id=mid, city_id=ny.id,
                                      event_date=DAY, hours_to_close=10.0, local_hour=12,
                                      model_p=.2, raw_p=.2, normalized=False))
            await db.commit()
        try:
            resp = await admin_shadow_csv("t", days=5)
            body = "".join([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
            async with maker() as db:
                kept = len((await db.execute(select(ShadowSnapshot))).scalars().all())
        finally:
            await engine.dispose()
        assert "b10" in body and "b20" not in body
        assert kept == 2, "nothing is deleted"
