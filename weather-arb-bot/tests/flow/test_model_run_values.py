"""Each model's forecast high, recorded whenever it changes — through the real
Open-Meteo job against the simulator — and the two research exports."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.workers.open_meteo_job as om
from app.models.market import Market, MarketOutcome, MarketPrice
from app.models.model_run_value import ModelRunValue
from tests.fixtures import cities as city_fixtures
from tests.flow.test_open_meteo_batch import _make_world, _run_batched
from tests.flow.test_open_meteo_updates import T0, _ShiftableRun
from tests.mocks import sqlite_compat


async def _values(maker):
    async with maker() as db:
        return (await db.execute(select(ModelRunValue).order_by(ModelRunValue.id))).scalars().all()


def _core_sources():
    return {s.source for s in om.specs_for("core")}


class TestRecording:
    @pytest.mark.asyncio
    async def test_the_first_fetch_is_a_baseline_for_the_tradeable_dates_only(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            await _run_batched(w.maker, tiers=["core"], now=T0)
            vals = await _values(w.maker)
        finally:
            await w.engine.dispose()
        today = date.today()
        assert vals, "values are recorded"
        assert {v.source for v in vals} == _core_sources()
        assert all(v.prev_fetch_at is None for v in vals), "nothing to compare with yet"
        assert {v.forecast_for_date for v in vals} <= {today + timedelta(days=i) for i in (-1, 0, 1, 2)}
        assert max(v.forecast_for_date for v in vals) == today + timedelta(days=2), \
            "the simulator answers 7 days; only up to two ahead are kept"

    @pytest.mark.asyncio
    async def test_an_unchanged_fetch_writes_nothing(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            await _run_batched(w.maker, tiers=["core"], now=T0)
            n = len(await _values(w.maker))
            await _run_batched(w.maker, tiers=["core"], now=T0 + timedelta(hours=1))
            assert len(await _values(w.maker)) == n
        finally:
            await w.engine.dispose()

    @pytest.mark.asyncio
    async def test_a_new_run_writes_the_unrounded_value_and_its_window(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            run = _ShiftableRun(http, date.today())
            await _run_batched(w.maker, tiers=["core"], now=T0)
            before = {(v.source, v.city_id, v.forecast_for_date): v.high_f for v in await _values(w.maker)}
            run.shift["gfs_seamless"] = 0.05        # moves no stored (rounded) forecast
            await _run_batched(w.maker, tiers=["core"], now=T0 + timedelta(hours=1))
            new = [v for v in await _values(w.maker) if v.prev_fetch_at is not None]
        finally:
            await w.engine.dispose()
        assert new and {v.source for v in new} == {"gfs"}, "only the model that changed"
        assert len(new) == 2 * 3, "two cities x three tradeable dates"
        for v in new:
            assert v.prev_fetch_at.replace(tzinfo=timezone.utc) == T0
            assert v.detected_at.replace(tzinfo=timezone.utc) == T0 + timedelta(hours=1)
            assert v.high_f == pytest.approx(before[("gfs", v.city_id, v.forecast_for_date)] + 0.05, abs=0.011)

    @pytest.mark.asyncio
    async def test_an_ensemble_records_its_unrounded_member_mean(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            monkeypatch.setattr("app.config.settings.open_meteo_extra_models", "")
            await _run_batched(w.maker, tiers=["ensemble"], now=T0.replace(hour=8))
            vals = [v for v in await _values(w.maker) if v.source.endswith("_ensemble")]
        finally:
            await w.engine.dispose()
        assert vals
        # five members at base-2 .. base+2: the mean is the base itself, unrounded
        from tests.mocks import weather_payloads as wx
        v = next(x for x in vals if x.source == "gfs_ensemble")
        assert v.high_f == pytest.approx(wx.open_meteo_value("ens_gfs_seamless", v.forecast_for_date), abs=0.01)

    @pytest.mark.asyncio
    async def test_old_values_are_pruned_at_midnight(self, http, monkeypatch):
        w = await _make_world(http, monkeypatch)
        try:
            async with w.maker() as db:
                db.add(ModelRunValue(source="gfs", city_id=1, forecast_for_date=date(2026, 7, 1),
                                     detected_at=T0 - timedelta(days=61), high_f=90.0))
                await db.commit()
            await _run_batched(w.maker, tiers=["core"], now=T0.replace(hour=0))
            vals = await _values(w.maker)
        finally:
            await w.engine.dispose()
        assert vals and all(v.detected_at.replace(tzinfo=timezone.utc) > T0 - timedelta(days=60) for v in vals)

    @pytest.mark.asyncio
    async def test_a_failure_here_does_not_cost_a_forecast(self, http, monkeypatch):
        """The measurement is optional; the forecasts are not."""
        w = await _make_world(http, monkeypatch)
        try:
            def _boom(*a, **k):
                raise RuntimeError("table missing")
            monkeypatch.setattr(om, "ModelRunValue", _boom)
            out = await _run_batched(w.maker, tiers=["core"], now=T0)
        finally:
            await w.engine.dispose()
        assert out["stored"] > 0 and out["errors"] == 0 and out["run_values"] == 0


async def _export_db(monkeypatch):
    from app.database import Base
    import app.models  # noqa: F401
    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    monkeypatch.setattr("app.utils.csv_stream.AsyncSessionLocal", maker)
    return engine, maker


async def _body(resp) -> str:
    raw = b"".join([c if isinstance(c, bytes) else c.encode() async for c in resp.body_iterator])
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        return zf.read(zf.namelist()[0]).decode()
    except zipfile.BadZipFile:
        return raw.decode()


class TestExports:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("zipped", [False, True], ids=["csv", "zip"])
    async def test_model_runs_from_a_date(self, monkeypatch, zipped):
        from app.api.admin import admin_model_runs_csv
        engine, maker = await _export_db(monkeypatch)
        austin = city_fixtures.get("Austin")
        today = datetime.now(timezone.utc).date()
        async with maker() as db:
            db.add(austin.as_row())
            for ago, high in ((1, 95.25), (10, 91.5)):
                at = datetime.combine(today - timedelta(days=ago), datetime.min.time(), tzinfo=timezone.utc)
                db.add(ModelRunValue(source="icon", city_id=austin.id, forecast_for_date=today,
                                     detected_at=at + timedelta(hours=6),
                                     prev_fetch_at=at + timedelta(hours=5), high_f=high))
            await db.commit()
        try:
            body = await _body(await admin_model_runs_csv(
                "t", days=30, zipped=zipped, since=today - timedelta(days=3)))
        finally:
            await engine.dispose()
        lines = body.strip().splitlines()
        assert lines[0] == "detected_at,prev_fetch_at,source,city,forecast_for_date,high_f"
        assert len(lines) == 2 and "icon,Austin" in lines[1] and "95.25" in lines[1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("zipped", [False, True], ids=["csv", "zip"])
    async def test_price_history_of_the_markets_from_a_date(self, monkeypatch, zipped):
        from app.api.admin import admin_prices_csv
        engine, maker = await _export_db(monkeypatch)
        austin = city_fixtures.get("Austin")
        today = date.today()
        async with maker() as db:
            db.add(austin.as_row())
            for mid, ev in ((1, today - timedelta(days=1)), (2, today - timedelta(days=20))):
                db.add(Market(id=mid, city_id=austin.id, external_id=f"m{mid}", question="q", event_date=ev))
                db.add(MarketOutcome(id=mid * 10, market_id=mid, bucket_label=f"b{mid}", bucket_unit="F",
                                     won=True))
                for k in range(3):
                    db.add(MarketPrice(outcome_id=mid * 10, yes_price=0.40 + k / 10, no_price=0.60 - k / 10,
                                       timestamp=datetime.now(timezone.utc) - timedelta(days=2, minutes=5 * k)))
            await db.commit()
        try:
            body = await _body(await admin_prices_csv("t", days=7, zipped=zipped,
                                                      since=today - timedelta(days=5)))
        finally:
            await engine.dispose()
        lines = body.strip().splitlines()
        assert lines[0] == "timestamp,city,event_date,market_id,outcome_id,bucket,yes_price,won"
        rows = lines[1:]
        assert len(rows) == 3, "three price changes of the recent market; the old one left out"
        assert all(",b1," in r and r.endswith(",1") for r in rows)
        stamps = [r.split(",")[0] for r in rows]
        assert stamps == sorted(stamps), "in time order"
