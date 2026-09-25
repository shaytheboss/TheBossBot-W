"""The shadow study's intraday probability, on the pipeline harness.

The world: Austin, market resolving today, every source forecasting 95°F,
and the thermometer already at 96°F — Wunderground 96, METAR 95.9. (WU at or
above METAR is what lets the estimator treat the max as confirmed; a METAR
reading above WU is deliberately not trusted for a lock.) The daily
model cannot see that; the intraday model can — which is the whole reason
the study records both.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.intraday.detector as idet
import app.shadow.estimate as est
import app.shadow.models  # noqa: F401  — registers the tables
from app.intraday.estimator import DEFAULT_PARAMS, PROB_LO
from app.models.city import City
from app.models.forecast import Forecast
from app.models.market import Market, MarketOutcome
from app.shadow.models import ShadowSnapshot
from app.shadow.snapshot import is_dead, job_shadow_snapshot
from tests.mocks import weather_payloads as wx

LOC_HOUR = 16.5


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


async def _world(pipeline, *, metar_f: float = 95.9, wu_f: float = 96.0, metar=True):
    await pipeline.collect_forecasts()
    await pipeline.collect_ensemble()
    await pipeline.collect_prices()
    if metar:
        pipeline.http.prepend(wx.METAR, wx.metar_records(temp_c=_f_to_c(metar_f)))
        await pipeline.collect_metar()
        async with pipeline.session() as db:
            db.add(Forecast(city_id=pipeline.city.id, source="wunderground",
                            forecast_for_date=pipeline.event_date, predicted_high_f=wu_f,
                            retrieved_at=datetime.now(timezone.utc)))
            await db.commit()


@pytest.fixture
def intraday_hours(monkeypatch):
    """Pin the local hour inside the intraday window. The date and hour gates
    themselves are tested with explicit clocks in TestGates."""
    monkeypatch.setattr(est, "intraday_hour", lambda city, market, now, params: LOC_HOUR)


async def _estimates(pipeline, now=None):
    async with pipeline.session() as db:
        city = (await db.execute(select(City))).scalars().one()
        market = (await db.execute(select(Market))).scalars().one()
        outcomes = (await db.execute(select(MarketOutcome).order_by(MarketOutcome.id))).scalars().all()
        ests = await est.estimate_market(db, city, market, outcomes,
                                         now or datetime.now(timezone.utc), today=date.today())
        labels = {o.id: o.bucket_label for o in outcomes}
    return {labels[e.outcome_id]: e for e in ests}


@pytest.mark.parametrize("days_ahead", [0], indirect=True)
class TestTheIntradayProbability:
    @pytest.mark.asyncio
    async def test_it_sees_the_thermometer_the_daily_model_cannot(self, pipeline, intraday_hours):
        """96°F already measured: 93-94°F is impossible. The daily model,
        forecasting 95°F with a 4°F spread, still gives it a real chance."""
        await _world(pipeline)
        e = await _estimates(pipeline)
        assert e["93-94°F"].intraday_p == pytest.approx(PROB_LO)
        assert e["93-94°F"].model_p > 0.10
        assert e["95-96°F"].intraday_p > 0.5 > e["95-96°F"].model_p

    @pytest.mark.asyncio
    async def test_it_is_the_number_the_intraday_detector_computes(self, pipeline, intraday_hours):
        """Same database, same moment: the study's intraday_p equals the
        probability the real detector stores on its position."""
        await _world(pipeline)
        now = datetime.now(timezone.utc)
        shadow = await _estimates(pipeline, now)
        async with pipeline.session() as db:
            city = (await db.execute(select(City))).scalars().one()
            market = (await db.execute(select(Market))).scalars().one()
            outcome = (await db.execute(select(MarketOutcome).where(
                MarketOutcome.bucket_label == "95-96°F"))).scalars().one()
            tz = ZoneInfo(city.timezone)
            minutes = await idet._minutes_since_running_max(db, city.primary_icao, tz, now)
            opp, _ = await idet._evaluate_intraday_outcome(
                db=db, city=city, market=market, outcome=outcome, tz=tz,
                loc_hour=LOC_HOUR, minutes_since_max=minutes, params=DEFAULT_PARAMS,
                alert_thresh=0.0, buy_thresh=0.0, min_edge=-1.0, max_edge=2.0,
                max_spread=1.0, shares=5,
            )
        assert opp is not None
        assert float(opp.estimated_true_prob) == pytest.approx(shadow["95-96°F"].intraday_p, abs=1e-4)

    @pytest.mark.asyncio
    async def test_it_is_written_on_every_recorded_row(self, pipeline, intraday_hours):
        await _world(pipeline)
        stats = await job_shadow_snapshot(session_factory=pipeline.session,
                                          now=datetime.now(timezone.utc), today=date.today())
        async with pipeline.session() as db:
            rows = (await db.execute(select(ShadowSnapshot))).scalars().all()
        assert stats["rows"] == len(rows) > 0
        assert all(r.intraday_p is not None for r in rows)

    @pytest.mark.asyncio
    async def test_without_a_metar_reading_there_is_none_and_the_row_is_kept(
            self, pipeline, intraday_hours):
        await _world(pipeline, metar=False)
        e = await _estimates(pipeline)
        assert e and all(v.intraday_p is None for v in e.values())

    @pytest.mark.asyncio
    async def test_a_failing_intraday_estimate_does_not_lose_the_daily_one(
            self, pipeline, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("intraday broke")
        monkeypatch.setattr(est, "intraday_probabilities", boom)
        await _world(pipeline)
        e = await _estimates(pipeline)
        assert e and all(v.intraday_p is None and v.model_p > 0 for v in e.values())

    @pytest.mark.asyncio
    async def test_it_registers_no_cluster_warmup(self, pipeline, intraday_hours):
        """The detector records a city running above forecast so its sister
        cities get a bias boost. The study must only read that state."""
        await _world(pipeline, metar_f=104.0, wu_f=104.0)     # far above the 95°F forecast
        before = dict(idet._cluster_warmth_today)
        await _estimates(pipeline)
        assert idet._cluster_warmth_today == before


class TestGates:
    """The two gates detect_intraday applies, with explicit clocks."""

    CITY = SimpleNamespace(timezone="America/Chicago")
    DAY = date(2026, 9, 24)

    def _at(self, local_hour: int, day: date = DAY) -> datetime:
        return datetime(day.year, day.month, day.day, local_hour,
                        tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc)

    def test_inside_the_window_it_returns_the_local_hour(self):
        m = SimpleNamespace(event_date=self.DAY)
        assert est.intraday_hour(self.CITY, m, self._at(15), DEFAULT_PARAMS) == pytest.approx(15.0)

    def test_before_the_intraday_start_it_does_not_run(self):
        m = SimpleNamespace(event_date=self.DAY)
        assert est.intraday_hour(self.CITY, m, self._at(8), DEFAULT_PARAMS) is None

    def test_a_market_for_another_local_day_is_not_intraday(self):
        """21:00 in Chicago is already tomorrow in UTC — the city's own date
        decides, not the server's."""
        tomorrow = SimpleNamespace(event_date=self.DAY + timedelta(days=1))
        today = SimpleNamespace(event_date=self.DAY)
        at = self._at(21)
        assert at.date() == self.DAY + timedelta(days=1)
        assert est.intraday_hour(self.CITY, tomorrow, at, DEFAULT_PARAMS) is None
        assert est.intraday_hour(self.CITY, today, at, DEFAULT_PARAMS) == pytest.approx(21.0)


class TestDeadBuckets:
    def test_an_intraday_view_keeps_a_bucket_the_others_dismissed(self):
        assert is_dead(0.01, 0.01) is True
        assert is_dead(0.01, 0.01, intraday_p=0.40) is False
        assert is_dead(0.01, 0.01, intraday_p=0.01) is True
