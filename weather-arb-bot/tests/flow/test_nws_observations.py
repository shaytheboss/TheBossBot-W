"""weather.gov observations for US cities, through the real job against a
mocked api.weather.gov — and the export."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.nwsobs.job as job
import app.nwsobs.models  # noqa: F401  — registers the table
from app.nwsobs.models import NwsObservation
from tests.fixtures import cities as city_fixtures
from tests.mocks import sqlite_compat
from tests.mocks import weather_payloads as wx

T0 = datetime(2026, 11, 16, 15, 3, tzinfo=timezone.utc)


def _readings(start: datetime, temps_c, metar_every: int = 12):
    """Five-minute readings ending at `start`, every `metar_every`-th a METAR."""
    n = len(temps_c)
    return [(start - timedelta(minutes=5 * (n - 1 - i)), t, i % metar_every == 0)
            for i, t in enumerate(temps_c)]


@pytest.fixture
async def world(monkeypatch):
    from app.database import Base
    import app.models  # noqa: F401
    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    async with maker() as db:
        db.add_all(city_fixtures.rows("Austin", "Dallas", "London"))
        await db.commit()
    monkeypatch.setattr(job, "AsyncSessionLocal", maker)
    monkeypatch.setattr("app.utils.csv_stream.AsyncSessionLocal", maker)
    yield maker
    await engine.dispose()


async def _stored(maker):
    async with maker() as db:
        return (await db.execute(select(NwsObservation).order_by(
            NwsObservation.icao, NwsObservation.observed_at))).scalars().all()


async def _run(maker, now):
    async with maker() as db:
        return await job.record_all(db, now)


def _route(http, icao, readings):
    http.add(f"{wx.NWS_OBS}/{icao}/observations", json=wx.nws_observations(icao, readings))


class TestRecording:
    @pytest.mark.asyncio
    async def test_it_stores_every_reading_of_the_us_stations_only(self, http, world):
        _route(http, "KAUS", _readings(T0, [9, 9, 10, 10.6]))
        _route(http, "KDFW", _readings(T0, [11, 12]))
        stats = await _run(world, T0)
        rows = await _stored(world)
        assert stats["stations"] == 2 and stats["stored"] == 6 and stats["errors"] == 0
        assert {r.icao for r in rows} == {"KAUS", "KDFW"}
        assert http.count("EGLL") == 0, "London is not on api.weather.gov — never asked"
        kaus = [r for r in rows if r.icao == "KAUS"]
        assert [r.temperature_c for r in kaus] == [9, 9, 10, pytest.approx(10.6)]
        assert [r.is_metar for r in kaus] == [True, False, False, False]
        assert all(r.qc == "V" for r in kaus)

    @pytest.mark.asyncio
    async def test_the_next_run_adds_only_what_is_new(self, http, world):
        first = _readings(T0, [9, 10])
        _route(http, "KAUS", first)
        _route(http, "KDFW", [])
        await _run(world, T0)
        later = first + [(T0 + timedelta(minutes=5), 11, False)]
        http.prepend(f"{wx.NWS_OBS}/KAUS/observations", json=wx.nws_observations("KAUS", later))
        stats = await _run(world, T0 + timedelta(minutes=10))
        assert stats["stored"] == 1
        assert [r.temperature_c for r in await _stored(world)] == [9, 10, 11]

    @pytest.mark.asyncio
    async def test_a_missing_temperature_is_kept_as_null(self, http, world):
        _route(http, "KAUS", [(T0, None, False)])
        _route(http, "KDFW", [])
        await _run(world, T0)
        (r,) = await _stored(world)
        assert r.temperature_c is None and r.qc == "Z"

    @pytest.mark.asyncio
    async def test_one_failing_station_does_not_stop_the_others(self, http, world):
        http.add(f"{wx.NWS_OBS}/KAUS/observations", status=404, json={"title": "Not Found"})
        _route(http, "KDFW", _readings(T0, [12]))
        stats = await _run(world, T0)
        assert stats["errors"] == 1 and stats["stored"] == 1
        assert [r.icao for r in await _stored(world)] == ["KDFW"]

    @pytest.mark.asyncio
    async def test_old_rows_are_pruned_at_midnight(self, http, world):
        async with world() as db:
            db.add(NwsObservation(icao="KAUS", observed_at=T0 - timedelta(days=61), temperature_c=5,
                                  is_metar=True, fetched_at=T0 - timedelta(days=61)))
            await db.commit()
        _route(http, "KAUS", [])
        _route(http, "KDFW", [])
        await _run(world, T0.replace(hour=0, minute=3))
        assert await _stored(world) == []

    @pytest.mark.asyncio
    async def test_the_job_can_be_switched_off(self, http, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.nws_obs_enabled", False)
        await job.job_record_nws_observations()
        assert http.count() == 0

    @pytest.mark.asyncio
    async def test_the_scheduled_job_runs_and_reports(self, http, world):
        _route(http, "KAUS", _readings(datetime.now(timezone.utc), [9]))
        _route(http, "KDFW", [])
        await job.job_record_nws_observations()
        assert job.LAST_RUN["stored"] == 1 and len(await _stored(world)) == 1


class TestStations:
    @pytest.mark.parametrize("icao,ok", [("KAUS", True), ("kdfw", True), ("EGLL", False),
                                         ("LFPB", False), ("ICAO", False), ("", False), (None, False)])
    def test_only_us_k_stations(self, icao, ok):
        assert job.is_us_station(icao) is ok


class TestExport:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("zipped", [False, True], ids=["csv", "zip"])
    async def test_it_exports_from_a_date_with_the_city(self, http, world, zipped):
        from app.api.admin import admin_nws_obs_csv
        from tests.flow.test_model_run_values import _body
        now = datetime.now(timezone.utc).replace(microsecond=0)
        async with world() as db:
            for ago, t, m in ((1, 10.6, True), (1.1, 11, False), (9, 4, False)):
                db.add(NwsObservation(icao="KAUS", observed_at=now - timedelta(days=ago),
                                      temperature_c=t, is_metar=m, qc="V", fetched_at=now))
            await db.commit()
        body = await _body(await admin_nws_obs_csv("t", days=14, zipped=zipped,
                                                   since=(now - timedelta(days=3)).date()))
        lines = body.strip().splitlines()
        assert lines[0] == "observed_at,icao,city,temperature_c,is_metar,qc"
        assert len(lines) == 3 and all(",KAUS,Austin," in ln for ln in lines[1:])
        assert {ln.split(",")[4] for ln in lines[1:]} == {"0", "1"}
