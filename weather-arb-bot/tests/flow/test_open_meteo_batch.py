"""Open-Meteo batched fetching, driven through the real jobs.

Mocked Open-Meteo (a simulator that behaves like the API: values depend on
model and date, never on how many days were requested) → the real legacy jobs
and the real batched job → a temporary SQLite database. The central claim is
that the batched path writes the same rows with a seventh of the requests.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

import app.workers.open_meteo_job as om
from app.models.city import City
from app.models.forecast import Forecast
from tests.fixtures import cities as city_fixtures
from tests.mocks import sqlite_compat
from tests.mocks import weather_payloads as wx

CORE_TIERS = ["core", "hrrr", "ensemble"]      # what the legacy jobs fetch


async def _make_world(http, monkeypatch, *, start_offset=0, horizons=None,
                      rejected=(), names=("Austin", "London")):
    from app.database import Base
    import app.models  # noqa: F401

    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    async with maker() as db:
        db.add_all(city_fixtures.rows(*names))
        await db.commit()
    for mod in ("app.workers.jobs", "app.workers.icon_job", "app.workers.open_meteo_job"):
        monkeypatch.setattr(f"{mod}.AsyncSessionLocal", maker)
    monkeypatch.setattr(om, "PACE_SECONDS", 0)
    sim = wx.open_meteo_simulator(date.today() + timedelta(days=start_offset),
                                  horizons=horizons or {"gfs_hrrr": 2}, rejected=rejected)
    http.add_handler(wx.OPEN_METEO, sim)
    http.add_handler(wx.OPEN_METEO_ENSEMBLE, sim)
    return SimpleNamespace(engine=engine, maker=maker, http=http)


@pytest.fixture
async def world(http, monkeypatch):
    w = await _make_world(http, monkeypatch)
    yield w
    await w.engine.dispose()


async def _rows(maker) -> set:
    async with maker() as db:
        fcs = (await db.execute(select(Forecast))).scalars().all()
    return {(f.city_id, f.source, f.forecast_for_date,
             float(f.predicted_high_f), float(f.predicted_low_f),
             json.dumps(f.raw_data, sort_keys=True)) for f in fcs}


async def _clear(maker):
    async with maker() as db:
        await db.execute(delete(Forecast))
        await db.commit()


async def _run_legacy(monkeypatch):
    from app.workers.icon_job import job_fetch_icon
    from app.workers.jobs import job_fetch_models
    monkeypatch.setattr("app.config.settings.model_fetch_mode", "legacy")
    await job_fetch_models()
    await job_fetch_icon()


async def _run_batched(maker, tiers=CORE_TIERS, now=None):
    async with maker() as db:
        cities = (await db.execute(select(City))).scalars().all()
        return await om.run_open_meteo(db, cities, tiers=tiers,
                                       now=now or datetime.now(timezone.utc).replace(hour=0))


# ── Equivalence ───────────────────────────────────────────────────────────

class TestSameRowsFewerRequests:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("start_offset", [-1, 0, 1],
                             ids=["city-behind-utc", "same-day", "city-ahead-of-utc"])
    async def test_batched_writes_exactly_the_rows_the_legacy_jobs_wrote(
            self, http, monkeypatch, start_offset):
        """Every row: same source, date, high, low and raw_data. Covers a city
        whose local calendar is a day behind or ahead of the server's."""
        w = await _make_world(http, monkeypatch, start_offset=start_offset)
        try:
            await _run_legacy(monkeypatch)
            legacy = await _rows(w.maker)
            await _clear(w.maker)
            monkeypatch.setattr("app.config.settings.model_fetch_mode", "batched")
            await _run_batched(w.maker)
            batched = await _rows(w.maker)
        finally:
            await w.engine.dispose()
        assert legacy, "the legacy path must have written something to compare against"
        assert batched == legacy
        sources = {r[1] for r in batched}
        assert sources == {"gfs", "ecmwf", "icon", "hrrr", "gfs_ensemble", "ecmwf_ensemble"}

    @pytest.mark.asyncio
    async def test_it_takes_a_seventh_of_the_requests(self, world, monkeypatch):
        await _run_legacy(monkeypatch)
        legacy = world.http.count()
        world.http.reset()
        await _run_batched(world.maker)
        batched = world.http.count()
        # Austin: 5 models x 7 days + HRRR x 3 days; London: 5 x 7.
        assert legacy == 35 + 3 + 35
        # One per (city, model): Austin 5 + HRRR, London 5.
        assert batched == 11

    @pytest.mark.asyncio
    async def test_hrrr_is_conus_only_and_stops_at_its_horizon(self, world):
        await _run_batched(world.maker, tiers=["hrrr"])
        rows = await _rows(world.maker)
        austin = city_fixtures.get("Austin").id
        assert {r[0] for r in rows} == {austin}, "London is outside HRRR's domain"
        assert len(world.http.calls_to(wx.OPEN_METEO)) == 1
        # The simulator returns HRRR through day index 2 and nulls after.
        assert {r[2] for r in rows} == {date.today() + timedelta(days=i) for i in range(3)}


# ── Switching modes ───────────────────────────────────────────────────────

class TestModes:
    @pytest.mark.asyncio
    async def test_in_batched_mode_the_legacy_jobs_send_nothing(self, world, monkeypatch):
        from app.workers.icon_job import job_fetch_icon
        from app.workers.jobs import job_fetch_models
        monkeypatch.setattr("app.config.settings.model_fetch_mode", "batched")
        await job_fetch_models()
        await job_fetch_icon()
        assert world.http.count() == 0

    @pytest.mark.asyncio
    async def test_in_legacy_mode_the_batched_job_sends_nothing(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.model_fetch_mode", "legacy")
        await om.job_fetch_open_meteo()
        assert world.http.count() == 0

    @pytest.mark.asyncio
    async def test_the_scheduled_job_fetches_in_batched_mode(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.model_fetch_mode", "batched")
        await om.job_fetch_open_meteo()
        assert world.http.count() > 0
        assert await _rows(world.maker)


# ── Rate limits ───────────────────────────────────────────────────────────

class TestRateLimit:
    @pytest.mark.asyncio
    async def test_a_429_stops_the_run_instead_of_retrying(self, world):
        world.http.prepend(wx.OPEN_METEO, {"error": True,
                                           "reason": "Hourly API request limit exceeded"},
                           status=429)
        now = datetime(2026, 9, 24, 10, 20, tzinfo=timezone.utc)
        out = await _run_batched(world.maker, now=now)
        assert world.http.count() == 1, "no retry, no next city"
        assert "429" in out["stopped"]
        assert om.BUDGET.paused_until == datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc)

        world.http.reset()
        again = await _run_batched(world.maker, now=now + timedelta(minutes=30))
        assert world.http.count() == 0 and "paused" in again["stopped"]

    @pytest.mark.asyncio
    async def test_a_daily_limit_pauses_until_midnight_utc(self, world):
        world.http.prepend(wx.OPEN_METEO, {"error": True,
                                           "reason": "Daily API request limit exceeded"},
                           status=429)
        now = datetime(2026, 9, 24, 10, 20, tzinfo=timezone.utc)
        await _run_batched(world.maker, now=now)
        assert om.BUDGET.paused_until == datetime(2026, 9, 25, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_a_server_error_is_retried_once_then_the_next_city_goes_on(
            self, world, no_backoff):
        world.http.prepend(wx.OPEN_METEO, {"error": "down"}, status=503)
        out = await _run_batched(world.maker, tiers=["core"])
        assert out["errors"] == 6 and out["stopped"] is None
        assert world.http.count() == 12, "two attempts for each of 3 models x 2 cities"


# ── Budget ────────────────────────────────────────────────────────────────

class TestBudget:
    @pytest.mark.asyncio
    async def test_lower_tiers_yield_to_the_core_tier(self, world, monkeypatch):
        """At 20:20 UTC the core tier still has three runs left today. The
        ensembles may not spend what those runs need."""
        now = datetime(2026, 9, 24, 20, 20, tzinfo=timezone.utc)
        core_run = 6.0                                   # 3 models x 2 cities
        monkeypatch.setattr("app.config.settings.open_meteo_daily_budget",
                            core_run * 4 + 1)            # this run + 3 more, + HRRR
        out = await _run_batched(world.maker, tiers=["core", "hrrr", "ensemble"], now=now)
        assert om.BUDGET.by_tier.get("core") == core_run
        assert "ensemble" not in om.BUDGET.by_tier
        assert out["skipped_budget"] >= 4

    @pytest.mark.asyncio
    async def test_every_request_is_counted_including_failures(self, world, no_backoff):
        world.http.prepend(wx.OPEN_METEO, {"error": "down"}, status=503)
        await _run_batched(world.maker, tiers=["core"])
        assert om.BUDGET.requests == world.http.count()

    @pytest.mark.asyncio
    async def test_the_counter_starts_over_on_a_new_utc_day(self, world):
        await _run_batched(world.maker, tiers=["core"],
                           now=datetime(2026, 9, 24, 23, 20, tzinfo=timezone.utc))
        assert om.BUDGET.used == 6
        await _run_batched(world.maker, tiers=["core"],
                           now=datetime(2026, 9, 25, 0, 20, tzinfo=timezone.utc))
        assert om.BUDGET.used == 6 and om.BUDGET.day == date(2026, 9, 25)


# ── Extra, record-only models ─────────────────────────────────────────────

class TestExtraModels:
    @pytest.mark.asyncio
    async def test_extra_models_are_stored_under_their_own_source(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.open_meteo_extra_models",
                            "ukmo_seamless,ncep_nbm_conus")
        await _run_batched(world.maker, tiers=["extra"])
        rows = await _rows(world.maker)
        by_source = {}
        for r in rows:
            by_source.setdefault(r[1], set()).add(r[0])
        austin, london = city_fixtures.get("Austin").id, city_fixtures.get("London").id
        assert by_source["om_ukmo_seamless"] == {austin, london}
        assert by_source["om_ncep_nbm_conus"] == {austin}, "NBM covers the US only"
        assert world.http.count() == 3

    @pytest.mark.asyncio
    async def test_a_rejected_model_is_dropped_for_the_day_and_the_rest_go_on(
            self, http, monkeypatch):
        w = await _make_world(http, monkeypatch, rejected=("no_such_model",))
        try:
            monkeypatch.setattr("app.config.settings.open_meteo_extra_models",
                                "no_such_model,jma_seamless")
            await _run_batched(w.maker, tiers=["extra"])
            assert "no_such_model" in om.BUDGET.rejected
            assert len([r for r in http.requests if r.url.params["models"] == "no_such_model"]) == 1
            assert {r[1] for r in await _rows(w.maker)} == {"om_jma_seamless"}

            http.reset()
            await _run_batched(w.maker, tiers=["extra"])
            assert all(r.url.params["models"] != "no_such_model" for r in http.requests)
        finally:
            await w.engine.dispose()

    def test_no_estimator_reads_an_extra_source(self):
        """The blend reads a fixed list of sources. An om_ source must not be
        on any of them, or recording a model would start trading on it."""
        from app.analyzers.beta_estimator import _DET_SOURCES as beta_sources
        from app.analyzers.model_skill import SKILL_SOURCES
        from app.analyzers.probability_estimator import _DET_SOURCES as prob_sources
        names = {om.extra_source(m) for m in om.extra_models()}
        keys = {k for k, *_ in prob_sources} | {k for k, *_ in beta_sources}
        assert names and not names & set(SKILL_SOURCES)
        assert not {f"{n}_forecast" for n in names} & keys


# ── Admin ─────────────────────────────────────────────────────────────────

class _DB:
    async def get(self, *a, **k): return None
    def add(self, *a): pass
    async def commit(self): pass


class TestAdmin:
    @pytest.mark.asyncio
    async def test_the_mode_is_switchable_and_persisted(self, monkeypatch):
        from app.api.admin import SettingsIn, admin_set_settings
        from app.config import settings
        from app.utils.settings_store import PERSISTABLE_KEYS
        monkeypatch.setattr(settings, "model_fetch_mode", "batched")
        await admin_set_settings(SettingsIn(model_fetch_mode="legacy"), "t", _DB())
        assert settings.model_fetch_mode == "legacy"
        assert {"model_fetch_mode", "open_meteo_extra_models"} <= PERSISTABLE_KEYS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [
        {"model_fetch_mode": "fast"},
        {"open_meteo_extra_models": "ukmo_seamless,https://evil"},
        {"open_meteo_extra_models": ",".join(f"m{i:02d}" for i in range(11))},
    ])
    async def test_bad_values_are_refused(self, payload):
        from fastapi import HTTPException
        from app.api.admin import SettingsIn, admin_set_settings
        with pytest.raises(HTTPException) as e:
            await admin_set_settings(SettingsIn(**payload), "t", _DB())
        assert e.value.status_code == 400

    @pytest.mark.asyncio
    async def test_status_reports_usage_and_the_plan(self, world):
        await _run_batched(world.maker, tiers=["core"])
        async with world.maker() as db:
            cities = (await db.execute(select(City))).scalars().all()
        s = om.status(cities)
        assert s["today"]["requests"] == 6 and s["last_run"]["stored"] > 0
        assert s["plan"]["legacy_per_day"] > s["plan"]["projected_per_day"]


# ── The plan at production scale ──────────────────────────────────────────

class TestPlan:
    @staticmethod
    def _fleet(n_us: int = 11, n_other: int = 37):
        us = [SimpleNamespace(nws_lat=30.2, nws_lon=-97.7) for _ in range(n_us)]
        other = [SimpleNamespace(nws_lat=51.5, nws_lon=-0.1) for _ in range(n_other)]
        return us + other

    def test_48_cities_fit_under_our_budget_at_the_defaults(self):
        p = om.plan(self._fleet())
        assert p["projected_per_day"] <= p["budget"] <= 10_000
        assert p["legacy_per_day"] > 40_000, "what the per-day loop costs today"

    def test_the_core_tier_runs_every_hour(self):
        assert all("core" in om.due_tiers(h) for h in range(24))
        assert sum("ensemble" in om.due_tiers(h) for h in range(24)) == 4
        assert sum("extra" in om.due_tiers(h) for h in range(24)) == 4

    def test_the_slow_tiers_do_not_share_an_hour(self):
        assert not any({"ensemble", "extra"} <= set(om.due_tiers(h)) for h in range(24))
