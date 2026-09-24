"""HRRR every 30 minutes, the hourly probe, and detecting when a model's
numbers changed — through the real job against the Open-Meteo simulator."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

import app.workers.open_meteo_job as om
from app.models.city import City
from app.models.model_update_event import ModelUpdateEvent
from tests.fixtures import cities as city_fixtures
from tests.flow.test_open_meteo_batch import _make_world, _run_batched
from tests.mocks import weather_payloads as wx

T0 = datetime(2026, 9, 24, 10, 20, tzinfo=timezone.utc)


@pytest.fixture
async def world(http, monkeypatch):
    w = await _make_world(http, monkeypatch)
    yield w
    await w.engine.dispose()


class _ShiftableRun:
    """Wraps the simulator so a test can publish a 'new model run': every
    value for the chosen models moves by `shift` degrees."""

    def __init__(self, http, start: date):
        self.shift: dict[str, float] = {}
        self.limited: set[str] = set()
        self._sim = wx.open_meteo_simulator(start, horizons={"gfs_hrrr": 2})
        http.prepend_handler(wx.OPEN_METEO_ENSEMBLE, self)
        http.prepend_handler(wx.OPEN_METEO, self)

    def __call__(self, request):
        if request.url.params.get("models", "") in self.limited:
            return httpx.Response(429, json={"error": True,
                                             "reason": "Hourly API request limit exceeded"})
        resp = self._sim(request)
        delta = self.shift.get(request.url.params.get("models", ""), 0.0)
        if not delta or resp.status_code != 200:
            return resp
        body = resp.json()
        block = body.get("daily") or body.get("hourly")
        for k, v in block.items():
            if k.startswith("temperature_2m"):
                block[k] = [None if x is None else x + delta for x in v]
        return httpx.Response(200, json=body)


async def _events(maker):
    async with maker() as db:
        return (await db.execute(select(ModelUpdateEvent).order_by(ModelUpdateEvent.id))).scalars().all()


# ── HRRR at :50 ───────────────────────────────────────────────────────────

class TestHalfHourlyHrrr:
    @pytest.mark.asyncio
    async def test_the_50_minute_run_fetches_hrrr_only(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.model_fetch_mode", "batched")
        await om.job_fetch_open_meteo_half_hour()
        models = {r.url.params["models"] for r in world.http.requests}
        assert models == {"gfs_hrrr"}
        assert len(world.http.requests) == 1, "Austin only; London is outside HRRR"

    @pytest.mark.asyncio
    async def test_it_can_be_switched_off(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.open_meteo_hrrr_half_hourly", False)
        await om.job_fetch_open_meteo_half_hour()
        assert world.http.count() == 0

    @pytest.mark.asyncio
    async def test_it_sends_nothing_in_legacy_mode(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.model_fetch_mode", "legacy")
        await om.job_fetch_open_meteo_half_hour()
        assert world.http.count() == 0


# ── The probe ─────────────────────────────────────────────────────────────

class TestProbe:
    @pytest.mark.asyncio
    async def test_slow_tiers_are_probed_for_one_city_between_their_runs(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.open_meteo_extra_models", "ukmo_seamless")
        await _run_batched(world.maker, tiers=["probe"], now=T0)   # 10:00 — neither slow tier due
        seen = sorted((r.url.params["models"], r.url.params["latitude"]) for r in world.http.requests)
        austin = city_fixtures.get("Austin")
        assert [m for m, _ in seen] == ["ecmwf_ifs025", "gfs_seamless", "ukmo_seamless"]
        assert {float(lat) for _, lat in seen} == {float(austin.lat)}, "one city: the lowest id"
        assert om.BUDGET.by_tier == {"probe": 11.0}

    @pytest.mark.asyncio
    async def test_no_probe_in_the_hour_the_full_tier_runs(self, world, monkeypatch):
        monkeypatch.setattr("app.config.settings.open_meteo_extra_models", "ukmo_seamless")
        at_ensemble_hour = T0.replace(hour=8)          # ensembles due, extras not
        await _run_batched(world.maker, tiers=["probe"], now=at_ensemble_hour)
        assert {r.url.params["models"] for r in world.http.requests} == {"ukmo_seamless"}

    def test_the_probe_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.open_meteo_probe_enabled", False)
        assert not any("probe" in om.due_tiers(h) for h in range(24))

    @pytest.mark.asyncio
    async def test_a_rejected_model_is_not_probed_either(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch, rejected=("no_such_model",))
        try:
            monkeypatch.setattr("app.config.settings.open_meteo_extra_models", "no_such_model")
            await _run_batched(w.maker, tiers=["probe"], now=T0)
            http.reset()
            await _run_batched(w.maker, tiers=["probe"], now=T0 + timedelta(hours=1))
            assert all(r.url.params["models"] != "no_such_model" for r in http.requests)
        finally:
            await w.engine.dispose()


# ── Change detection ──────────────────────────────────────────────────────

class TestChangeDetection:
    @pytest.mark.asyncio
    async def test_an_unchanged_fetch_records_nothing(self, world):
        await _run_batched(world.maker, tiers=["core"], now=T0)
        await _run_batched(world.maker, tiers=["core"], now=T0 + timedelta(hours=1))
        assert await _events(world.maker) == []

    @pytest.mark.asyncio
    async def test_a_new_run_is_recorded_with_the_window_it_appeared_in(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            run = _ShiftableRun(http, date.today())
            await _run_batched(w.maker, tiers=["core"], now=T0)
            # 0.05°F moves no stored (rounded) value — the simulator's values
            # end in .4 — so this is seen only if unrounded values are compared.
            run.shift["gfs_seamless"] = 0.05
            await _run_batched(w.maker, tiers=["core"], now=T0 + timedelta(hours=1))
            ev = await _events(w.maker)
        finally:
            await w.engine.dispose()
        assert [e.source for e in ev] == ["gfs"], "only the model that changed"
        e = ev[0]
        assert (e.cities_changed, e.cities_compared) == (2, 2)
        assert e.prev_fetch_at.replace(tzinfo=timezone.utc) == T0
        assert e.detected_at.replace(tzinfo=timezone.utc) == T0 + timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_changes_seen_before_a_429_are_still_recorded(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            run = _ShiftableRun(http, date.today())
            await _run_batched(w.maker, tiers=["core"], now=T0)
            run.shift["gfs_seamless"] = 1.0
            run.limited.add("ecmwf_ifs025")          # GFS is fetched first, then this
            out = await _run_batched(w.maker, tiers=["core"], now=T0 + timedelta(hours=1))
            ev = await _events(w.maker)
        finally:
            await w.engine.dispose()
        assert "429" in out["stopped"]
        assert [e.source for e in ev] == ["gfs"]

    @pytest.mark.asyncio
    async def test_the_first_fetch_after_a_restart_compares_with_nothing(self, world):
        await _run_batched(world.maker, tiers=["core"], now=T0)
        om._LAST_FETCH.clear()                       # what a deploy does
        await _run_batched(world.maker, tiers=["core"], now=T0 + timedelta(hours=1))
        assert await _events(world.maker) == []

    @pytest.mark.asyncio
    async def test_ensemble_changes_are_seen_through_the_probe(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            monkeypatch.setattr("app.config.settings.open_meteo_extra_models", "")
            run = _ShiftableRun(http, date.today())
            await _run_batched(w.maker, tiers=["probe"], now=T0)
            run.shift["ecmwf_ifs025"] = 1.0
            await _run_batched(w.maker, tiers=["probe"], now=T0 + timedelta(hours=1))
            async with w.maker() as db:
                report = await om.update_report(db, now=T0 + timedelta(hours=2))
        finally:
            await w.engine.dispose()
        assert list(report["sources"]) == ["ecmwf_ensemble"]
        r = report["sources"]["ecmwf_ensemble"]
        assert r["hours_utc"] == {11: 1}
        assert r["changes"][0]["cities"] == "1/1"

    @pytest.mark.asyncio
    async def test_old_events_are_pruned_at_midnight(self, world):
        async with world.maker() as db:
            db.add(ModelUpdateEvent(source="gfs", detected_at=T0 - timedelta(days=40),
                                    prev_fetch_at=T0 - timedelta(days=40, hours=1),
                                    cities_changed=1, cities_compared=1))
            await db.commit()
        await _run_batched(world.maker, tiers=["core"], now=T0.replace(hour=0))
        assert await _events(world.maker) == []

    @pytest.mark.asyncio
    async def test_a_failed_event_write_does_not_break_the_run(self, world, monkeypatch):
        """The measurement is optional; the forecasts are not."""
        def _boom(*a, **k):
            raise RuntimeError("table missing")
        await _run_batched(world.maker, tiers=["core"], now=T0)
        om._LAST_FETCH.update({k: (v[0], {d: 0 for d in v[1]}) for k, v in om._LAST_FETCH.items()})
        monkeypatch.setattr(om, "ModelUpdateEvent", _boom)
        out = await _run_batched(world.maker, tiers=["core"], now=T0 + timedelta(hours=1))
        assert out["stored"] > 0 and out["errors"] == 0


class TestAdmin:
    @pytest.mark.asyncio
    async def test_the_report_endpoint(self, world):
        from app.api.admin import admin_open_meteo_updates
        async with world.maker() as db:
            out = await admin_open_meteo_updates("t", db, days=3)
        assert out["sources"] == {} and out["days"] == 3

    @pytest.mark.asyncio
    async def test_the_toggles_persist(self, monkeypatch):
        from app.api.admin import SettingsIn, admin_set_settings
        from app.config import settings
        from app.utils.settings_store import PERSISTABLE_KEYS
        from tests.flow.test_open_meteo_batch import _DB
        monkeypatch.setattr(settings, "open_meteo_probe_enabled", True)
        await admin_set_settings(SettingsIn(open_meteo_probe_enabled=False), "t", _DB())
        assert settings.open_meteo_probe_enabled is False
        assert {"open_meteo_probe_enabled", "open_meteo_hrrr_half_hourly"} <= PERSISTABLE_KEYS

    def test_48_cities_with_both_additions_stay_under_budget(self):
        from tests.flow.test_open_meteo_batch import TestPlan
        p = om.plan(TestPlan._fleet())
        assert p["tiers"]["hrrr"]["runs_per_day"] == 48
        assert 0 < p["tiers"]["probe"]["per_day"] < 400
        assert p["projected_per_day"] <= p["budget"]
