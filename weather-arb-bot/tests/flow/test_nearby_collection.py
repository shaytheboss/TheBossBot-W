"""Nearby stations' METARs: who is fetched, when, and what is stored — through
the real job against a mocked aviationweather.gov — and the METAR export."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.nearby.job as job
from app.models.market import Market
from app.models.metar import MetarObservation
from app.nearby.stations import NEARBY, neighbours
from tests.fixtures import cities as city_fixtures
from tests.mocks import sqlite_compat
from tests.mocks import weather_payloads as wx

# 20:06 UTC = 14:06 in Austin (CST) — inside the window; 20:06 in London — outside.
T = datetime(2026, 11, 16, 20, 6, tzinfo=timezone.utc)
AUSTIN_NB = neighbours("KAUS")


def _batch(icaos, temp_c=22.0, at=T - timedelta(minutes=13)):
    return [wx.metar_records(i, temp_c=temp_c, observed=at)[0] for i in icaos]


@pytest.fixture
async def world(monkeypatch):
    from app.database import Base
    import app.models  # noqa: F401
    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    async with maker() as db:
        db.add_all(city_fixtures.rows("Austin", "London", "Dallas"))
        await db.flush()
        for i, name in enumerate(("Austin", "London", "Dallas")):
            c = city_fixtures.get(name)
            db.add(Market(id=i + 1, city_id=c.id, external_id=f"m{i}", question="q",
                          event_date=T.astimezone(ZoneInfo(c.tz)).date()))
        await db.commit()
    monkeypatch.setattr(job, "AsyncSessionLocal", maker)
    monkeypatch.setattr("app.utils.csv_stream.AsyncSessionLocal", maker)
    yield maker
    await engine.dispose()


async def _run(maker, now=T):
    async with maker() as db:
        return await job.record_nearby(db, now)


async def _stored(maker):
    async with maker() as db:
        return (await db.execute(select(MetarObservation).order_by(MetarObservation.icao))).scalars().all()


class TestWhoAndWhen:
    def test_the_table_is_keyed_by_settlement_station(self):
        assert len(AUSTIN_NB) == 5 and "KAUS" not in AUSTIN_NB
        assert neighbours("KDFW") == (), "Dallas settles on KDAL; the old station has no neighbours"
        assert neighbours(None) == () and neighbours("kaus") == AUSTIN_NB
        for target, rows in NEARBY.items():
            assert 1 <= len(rows) <= 5 and all(3 <= km <= 80 and 0 <= b < 360 for _, km, b in rows)

    @pytest.mark.asyncio
    async def test_one_request_for_the_cities_inside_their_window(self, http, world):
        http.add(wx.METAR, json=_batch(AUSTIN_NB))
        stats = await _run(world)
        assert http.count(wx.METAR) == 1
        assert set(http.param(wx.METAR, "ids").split(",")) == set(AUSTIN_NB), \
            "London is outside its window; Dallas's station has no neighbours"
        assert stats["cities"] == 1 and stats["stations"] == 5 and stats["stored"] == 5

    @pytest.mark.asyncio
    async def test_outside_every_window_nothing_is_fetched(self, http, world):
        stats = await _run(world, T.replace(hour=9))          # 03:06 in Austin
        assert http.count() == 0 and stats["stations"] == 0

    @pytest.mark.asyncio
    async def test_no_open_market_nothing_is_fetched(self, http, world):
        async with world() as db:
            m = await db.get(Market, 1)
            m.resolved = True
            await db.commit()
        stats = await _run(world)
        assert http.count() == 0 and stats["stations"] == 0

    @pytest.mark.asyncio
    async def test_switched_off_sends_nothing(self, http, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.nearby_obs_enabled", False)
        await job.job_record_nearby()
        assert http.count() == 0


class TestWhatIsStored:
    @pytest.mark.asyncio
    async def test_readings_land_in_metar_observations_once(self, http, world):
        http.add(wx.METAR, json=_batch(AUSTIN_NB, temp_c=22.0))
        await _run(world)
        first = await _stored(world)
        await _run(world, T + timedelta(minutes=10))          # same readings again
        again = await _stored(world)
        assert len(first) == len(again) == 5
        assert {r.icao for r in again} == set(AUSTIN_NB)
        assert all(float(r.temperature_f) == pytest.approx(71.6) for r in again)

    @pytest.mark.asyncio
    async def test_a_reading_without_temperature_is_skipped(self, http, world):
        recs = _batch(AUSTIN_NB[:2])
        recs[0]["temp"] = None
        http.add(wx.METAR, json=recs)
        stats = await _run(world)
        assert stats["readings"] == 2 and stats["stored"] == 1

    @pytest.mark.asyncio
    async def test_a_failure_is_reported_not_raised(self, http, world):
        http.add(wx.METAR, status=404, json={"error": "nope"})
        stats = await _run(world)
        assert stats["error"] and stats["stored"] == 0
        assert await _stored(world) == []

    @pytest.mark.asyncio
    async def test_the_scheduled_job_reports(self, http, world, monkeypatch):
        http.add(wx.METAR, json=_batch(AUSTIN_NB))
        monkeypatch.setattr(job, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: T)}))
        await job.job_record_nearby()
        assert job.LAST_RUN["stored"] == 5


class TestExport:
    @pytest.mark.asyncio
    async def test_each_station_is_labelled_with_city_and_role(self, world):
        from app.api.admin import admin_metar_csv
        from tests.flow.test_model_run_values import _body
        now = datetime.now(timezone.utc).replace(microsecond=0)
        async with world() as db:
            for icao in ("KAUS", AUSTIN_NB[0], "KDFW"):
                db.add(MetarObservation(icao=icao, observed_at=now - timedelta(hours=2), temperature_f=70.0))
            db.add(MetarObservation(icao="KAUS", observed_at=now - timedelta(days=20), temperature_f=60.0))
            await db.commit()
        body = await _body(await admin_metar_csv("t", days=14, zipped=True, since=None))
        lines = body.strip().splitlines()
        assert lines[0] == "observed_at,icao,city,role,temperature_f,wind_direction,wind_speed_kt"
        got = {(ln.split(",")[1], ln.split(",")[3]) for ln in lines[1:]}
        assert ("KAUS", "settlement") in got and (AUSTIN_NB[0], "nearby") in got
        assert ("KDFW", "settlement") in got, "Dallas's own station (fixture) is exported"
        assert len(lines) == 4, "the reading from 20 days ago is outside the window"
