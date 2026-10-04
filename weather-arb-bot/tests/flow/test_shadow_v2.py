"""Model v2: per-city bias correction and accuracy weighting, record-only."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.peaks.models  # noqa: F401
import app.shadow.models  # noqa: F401
from app.models.forecast import Forecast
from app.peaks.models import DailyPeak
from app.shadow import v2

TODAY = date(2026, 10, 10)


def _synthetic(days=20, gfs_bias=3.0, ecmwf_bias=-1.0, seed=1):
    rnd = random.Random(seed)
    truth, fc = {}, {}
    for i in range(1, days + 1):
        d = TODAY - timedelta(days=i)
        t = 80 + rnd.uniform(-5, 5)
        truth[d] = t
        for lead in (0, 1, 2):
            fc[("gfs", d, lead)] = t + gfs_bias + rnd.gauss(0, 2.0)     # biased and noisy
            fc[("ecmwf", d, lead)] = t + ecmwf_bias + rnd.gauss(0, 0.5)  # biased, accurate
    return truth, fc


class TestCalibrate:
    def test_each_models_bias_is_learned(self):
        cal = v2.calibrate(*_synthetic())
        assert cal.models[("gfs", 1)][0] == pytest.approx(3.0, abs=1.0)
        assert cal.models[("ecmwf", 1)][0] == pytest.approx(-1.0, abs=0.4)

    def test_the_accurate_model_gets_the_smaller_error(self):
        cal = v2.calibrate(*_synthetic())
        assert cal.models[("ecmwf", 1)][1] < cal.models[("gfs", 1)][1]

    def test_too_few_days_calibrates_nothing(self):
        cal = v2.calibrate(*_synthetic(days=v2.MIN_SAMPLES - 1))
        assert cal.models == {} and cal.sigma == {}

    def test_the_spread_never_goes_below_the_floor(self):
        truth = {TODAY - timedelta(days=i): 80.0 for i in range(1, 15)}
        fc = {("ecmwf", d, 1): 80.0 for d in truth}
        assert v2.calibrate(truth, fc).sigma[1] == v2.SIGMA_FLOOR_F

    def test_the_corrected_average_beats_the_plain_one(self):
        """The point of v2: on fresh days, correcting each model's bias and
        weighting by accuracy lands closer than a plain average."""
        cal = v2.calibrate(*_synthetic(seed=1))
        truth, fc = _synthetic(days=20, seed=99)          # unseen days, same biases
        plain = corrected = 0.0
        for d, t in truth.items():
            sig = {"gfs_forecast": {"predicted_high_f": fc[("gfs", d, 1)]},
                   "ecmwf_forecast": {"predicted_high_f": fc[("ecmwf", d, 1)]}}
            mu, _ = v2.mean_and_sigma(cal, sig, 1)
            corrected += abs(mu - t)
            plain += abs((fc[("gfs", d, 1)] + fc[("ecmwf", d, 1)]) / 2 - t)
        assert corrected < 0.5 * plain


class TestProbability:
    def test_celsius_bucket_edges(self):
        lo, hi = v2.bucket_bounds_f(35, 35, "C")
        assert lo == pytest.approx(94.1) and hi == pytest.approx(95.9)

    def test_a_full_ladder_sums_to_one(self):
        ladder = [(None, 89)] + [(t, t + 1) for t in range(90, 100, 2)] + [(100, None)]
        total = sum(v2.probability(94.3, 2.0, *v2.bucket_bounds_f(a, b, "F")) for a, b in ladder)
        assert total == pytest.approx(1.0, abs=1e-6)

    def test_most_mass_lands_on_the_bucket_holding_the_mean(self):
        p_in = v2.probability(94.6, 1.2, *v2.bucket_bounds_f(94, 95, "F"))
        p_far = v2.probability(94.6, 1.2, *v2.bucket_bounds_f(100, 101, "F"))
        assert p_in > 0.5 and p_far < 0.01

    def test_lead_is_clamped_to_the_calibrated_range(self):
        cal = v2.calibrate(*_synthetic())
        sig = {"ecmwf_forecast": {"predicted_high_f": 81.0}}
        assert v2.mean_and_sigma(cal, sig, 5)[1] == cal.sigma[2]


async def _history(db, city_id, days=20, gfs_bias=3.0):
    rnd = random.Random(3)
    for i in range(1, days + 1):
        d = TODAY - timedelta(days=i)
        t = 80 + rnd.uniform(-4, 4)
        db.add(DailyPeak(city_id=city_id, local_date=d, icao="KAUS", max_f=t, peak_hour=15,
                         peak_last_hour=15, n_obs=24, first_obs_hour=0, last_obs_hour=23,
                         max_gap_h=1, spike_dropped=0,
                         computed_at=datetime.now(timezone.utc)))
        for lead in (0, 1, 2):
            made = datetime.combine(d - timedelta(days=lead), time(12), tzinfo=timezone.utc)
            db.add(Forecast(city_id=city_id, source="gfs", forecast_for_date=d,
                            predicted_high_f=t + gfs_bias + rnd.gauss(0, 0.3), retrieved_at=made))
    await db.commit()


class TestFromTheDatabase:
    @pytest.mark.asyncio
    async def test_calibration_reads_peaks_and_forecasts(self, sqlite_db):
        await _history(sqlite_db, 1)
        cal = await v2.city_calibration(sqlite_db, 1, TODAY)
        assert cal.models[("gfs", 1)][0] == pytest.approx(3.0, abs=0.3)
        assert 1 in cal.sigma

    @pytest.mark.asyncio
    async def test_it_is_computed_once_a_day(self, sqlite_db):
        await _history(sqlite_db, 1)
        await v2.city_calibration(sqlite_db, 1, TODAY)
        calls = []
        real = sqlite_db.execute

        async def counting(*a, **k):
            calls.append(1)
            return await real(*a, **k)
        sqlite_db.execute = counting
        await v2.city_calibration(sqlite_db, 1, TODAY)
        assert calls == []

    @pytest.mark.asyncio
    async def test_market_probabilities_follow_the_corrected_forecast(self, sqlite_db):
        """GFS runs 3°F hot here; it says 98, so v2 centres on ~95."""
        await _history(sqlite_db, 1)
        outcomes = [SimpleNamespace(id=i, bucket_min=lo, bucket_max=hi, bucket_unit="F",
                                    bucket_label=f"{lo}-{hi}")
                    for i, (lo, hi) in enumerate([(92, 93), (94, 95), (96, 97), (98, 99)])]
        p, mu = await v2.v2_for_market(sqlite_db, SimpleNamespace(id=1), None, outcomes,
                                       {"gfs_forecast": {"predicted_high_f": 98.0}}, 1, TODAY)
        assert mu == pytest.approx(95.0, abs=0.4)
        assert max(p, key=p.get) == 1, "94-95 is the favourite, not GFS's raw 98-99"

    @pytest.mark.asyncio
    async def test_an_uncalibrated_city_has_no_v2(self, sqlite_db):
        p, mu = await v2.v2_for_market(sqlite_db, SimpleNamespace(id=7), None, [], {}, 1, TODAY)
        assert p == {} and mu is None


@pytest.mark.parametrize("days_ahead", [1], indirect=True)
class TestInTheShadowStudy:
    @pytest.mark.asyncio
    async def test_snapshots_carry_v2_once_the_city_is_calibrated(self, pipeline, monkeypatch):
        from sqlalchemy import select
        from app.shadow.models import ShadowSnapshot
        from app.shadow.snapshot import job_shadow_snapshot
        monkeypatch.setattr(v2, "_CACHE", {})
        await pipeline.collect_forecasts(); await pipeline.collect_ensemble(); await pipeline.collect_prices()
        today = date.today()
        async with pipeline.session() as db:
            rnd = random.Random(5)
            for i in range(1, 20):
                d = today - timedelta(days=i)
                t = 94 + rnd.uniform(-2, 2)
                db.add(DailyPeak(city_id=pipeline.city.id, local_date=d, icao="KAUS", max_f=t,
                                 peak_hour=15, peak_last_hour=15, n_obs=24, first_obs_hour=0,
                                 last_obs_hour=23, max_gap_h=1, spike_dropped=0,
                                 computed_at=datetime.now(timezone.utc)))
                for src in ("gfs", "ecmwf"):
                    db.add(Forecast(city_id=pipeline.city.id, source=src, forecast_for_date=d,
                                    predicted_high_f=t + rnd.gauss(0, 1),
                                    retrieved_at=datetime.combine(d - timedelta(days=1), time(12),
                                                                  tzinfo=timezone.utc)))
            await db.commit()
        now = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)
        await job_shadow_snapshot(session_factory=pipeline.session, now=now, today=today)
        async with pipeline.session() as db:
            rows = (await db.execute(select(ShadowSnapshot))).scalars().all()
        assert rows and all(r.v2_p is not None and r.v2_mu is not None for r in rows)

    @pytest.mark.asyncio
    async def test_a_failing_v2_never_costs_the_daily_row(self, pipeline, monkeypatch):
        from sqlalchemy import select
        from app.shadow.models import ShadowSnapshot
        from app.shadow.snapshot import job_shadow_snapshot

        async def boom(*a, **k):
            raise RuntimeError("v2 broke")
        monkeypatch.setattr(v2, "v2_for_market", boom)
        await pipeline.collect_forecasts(); await pipeline.collect_ensemble(); await pipeline.collect_prices()
        now = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)
        stats = await job_shadow_snapshot(session_factory=pipeline.session, now=now, today=date.today())
        async with pipeline.session() as db:
            rows = (await db.execute(select(ShadowSnapshot))).scalars().all()
        assert stats["rows"] > 0 and all(r.v2_p is None for r in rows)

    @pytest.mark.asyncio
    async def test_it_can_be_switched_off(self, pipeline, monkeypatch):
        called = []

        async def spy(*a, **k):
            called.append(1)
            return {}, None
        monkeypatch.setattr(v2, "v2_for_market", spy)
        monkeypatch.setattr("app.config.settings.shadow_v2_enabled", False)
        from app.shadow.snapshot import job_shadow_snapshot
        await pipeline.collect_forecasts(); await pipeline.collect_prices()
        now = datetime.now(timezone.utc).replace(minute=5, second=0, microsecond=0)
        await job_shadow_snapshot(session_factory=pipeline.session, now=now, today=date.today())
        assert called == []


class TestForwardReport:
    @pytest.mark.asyncio
    async def test_v2_and_current_are_scored_on_the_same_live_rows(self, sqlite_db):
        from app.models.market import Market, MarketOutcome
        from app.shadow.models import ShadowSnapshot
        from app.shadow.v2_report import v2_forward_report
        ev = date(2026, 10, 8)
        sqlite_db.add(Market(id=1, city_id=1, external_id="m", question="q", event_date=ev, resolved=True))
        sqlite_db.add_all([MarketOutcome(id=1, market_id=1, bucket_label="a", bucket_unit="F", won=True),
                           MarketOutcome(id=2, market_id=1, bucket_label="b", bucket_unit="F", won=False)])
        t = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        for oid, v2p, cur in ((1, 0.60, 0.10), (2, 0.05, 0.70)):
            sqlite_db.add(ShadowSnapshot(outcome_id=oid, taken_at=t, market_id=1, city_id=1, event_date=ev,
                                         hours_to_close=15.0, local_hour=8, model_p=cur, raw_p=cur,
                                         normalized=False, market_p=0.3, bid=0.28, ask=0.32,
                                         price_live=True, v2_p=v2p))
        await sqlite_db.commit()
        out = await v2_forward_report(sqlite_db, date(2026, 10, 5))
        w = out["windows"]["morning of the event day (12-18 h before close)"]
        # v2: YES on the winner at 0.32 (+0.68) and NO on the loser at 0.72 (+0.28)
        assert w["v2, edge>0.10"]["entries"] == 2
        assert w["v2, edge>0.10"]["pnl_per_share"] == pytest.approx((0.68 + 0.28) / 2)
        # current: NO on the winner at 0.72 (-0.72) and YES on the loser at 0.32 (-0.32)
        assert w["current, edge>0.10"]["pnl_per_share"] == pytest.approx((-0.72 - 0.32) / 2)

    @pytest.mark.asyncio
    async def test_rows_before_the_date_are_ignored(self, sqlite_db):
        from app.shadow.v2_report import v2_forward_report
        out = await v2_forward_report(sqlite_db, date(2026, 10, 5))
        assert out["rows_considered"] == 0


class TestCalibrationDetails:
    @pytest.mark.asyncio
    async def test_each_lead_time_gets_its_own_bias(self, sqlite_db):
        """A model can run hotter two days out than on the day itself."""
        for i in range(1, 16):
            d = TODAY - timedelta(days=i)
            sqlite_db.add(DailyPeak(city_id=1, local_date=d, icao="KAUS", max_f=80.0, peak_hour=15,
                                    peak_last_hour=15, n_obs=24, first_obs_hour=0, last_obs_hour=23,
                                    max_gap_h=1, spike_dropped=0, computed_at=datetime.now(timezone.utc)))
            for lead, bias in ((0, 1.0), (2, 5.0)):
                sqlite_db.add(Forecast(city_id=1, source="gfs", forecast_for_date=d,
                                       predicted_high_f=80.0 + bias,
                                       retrieved_at=datetime.combine(d - timedelta(days=lead), time(12),
                                                                     tzinfo=timezone.utc)))
        await sqlite_db.commit()
        cal = await v2.city_calibration(sqlite_db, 1, TODAY)
        assert cal.models[("gfs", 0)][0] == pytest.approx(1.0)
        assert cal.models[("gfs", 2)][0] == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_an_incomplete_day_is_not_a_truth(self, sqlite_db):
        """A day read only in the morning has a 'high' far below the real one."""
        await _history(sqlite_db, 1, days=12)
        d = TODAY - timedelta(days=13)
        sqlite_db.add(DailyPeak(city_id=1, local_date=d, icao="KAUS", max_f=40.0, peak_hour=9,
                                peak_last_hour=9, n_obs=4, first_obs_hour=6, last_obs_hour=10,
                                max_gap_h=1, spike_dropped=0, computed_at=datetime.now(timezone.utc)))
        sqlite_db.add(Forecast(city_id=1, source="gfs", forecast_for_date=d, predicted_high_f=83.0,
                               retrieved_at=datetime.combine(d - timedelta(days=1), time(12),
                                                             tzinfo=timezone.utc)))
        await sqlite_db.commit()
        cal = await v2.city_calibration(sqlite_db, 1, TODAY)
        assert cal.models[("gfs", 1)][0] == pytest.approx(3.0, abs=0.3)

    @pytest.mark.asyncio
    async def test_a_new_day_recalibrates(self, sqlite_db):
        await _history(sqlite_db, 1)
        await v2.city_calibration(sqlite_db, 1, TODAY)
        calls = []
        real = sqlite_db.execute

        async def counting(*a, **k):
            calls.append(1)
            return await real(*a, **k)
        sqlite_db.execute = counting
        await v2.city_calibration(sqlite_db, 1, TODAY + timedelta(days=1))
        assert calls, "the next day reads the tables again"
