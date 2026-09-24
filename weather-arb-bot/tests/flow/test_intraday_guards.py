"""The two intraday guards, exercised through the REAL detector.

Existing gate tests re-implement the buy condition inline, so they would keep
passing if the detector's own logic changed. These call
_evaluate_intraday_outcome itself on the pipeline harness: real aggregator,
real estimator, real order-book read, real database row.

Both guards come from 5,418 settled intraday bets:

  lock margin      "impossible" locks won only 77.5%, and 58 of 67 were
                   declared when the max merely touched the bucket's edge
  entry floor      buying at an ask below 70c lost in both halves of a time
                   split (-6.35pp, t=-3.48; then -5.27pp, t=-2.77)

The world: Austin, market resolving TODAY, bucket 91-92°F (top edge 92.5°F).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.intraday.detector as idet
from app.intraday.estimator import DEFAULT_PARAMS
from app.models.forecast import Forecast
from app.models.intraday import IntradayOpportunity
from app.models.market import Market, MarketOutcome
from app.models.city import City
from tests.mocks import polymarket_payloads as pm


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


async def _world(pipeline, *, metar_f: float, wu_f: float):
    """Forecasts, prices, a METAR reading and a fresh Wunderground high."""
    from tests.mocks import weather_payloads as wx
    await pipeline.collect_forecasts()
    await pipeline.collect_prices()
    pipeline.http.prepend(wx.METAR, wx.metar_records(temp_c=_f_to_c(metar_f)))
    await pipeline.collect_metar()
    async with pipeline.session() as db:
        db.add(Forecast(city_id=pipeline.city.id, source="wunderground",
                        forecast_for_date=date.today(), predicted_high_f=wu_f,
                        retrieved_at=datetime.now(timezone.utc)))
        await db.commit()


async def _evaluate(pipeline, bucket_label="91-92°F"):
    async with pipeline.session() as db:
        city = (await db.execute(select(City))).scalars().one()
        market = (await db.execute(select(Market))).scalars().one()
        outcome = (await db.execute(select(MarketOutcome).where(
            MarketOutcome.bucket_label == bucket_label))).scalars().one()
        return await idet._evaluate_intraday_outcome(
            db=db, city=city, market=market, outcome=outcome,
            tz=ZoneInfo(city.timezone), loc_hour=16.5, minutes_since_max=120.0,
            params=DEFAULT_PARAMS, alert_thresh=0.90, buy_thresh=0.94,
            min_edge=0.05, max_edge=0.40, max_spread=0.10, shares=5,
        )


@pytest.mark.parametrize("days_ahead", [0], indirect=True)
class TestLockMargin:
    @pytest.mark.asyncio
    async def test_a_max_clearly_past_the_edge_is_a_lock_and_is_bought(self, pipeline):
        """94.0°F is 1.5°F past 92.5 — beyond METAR's resolution. The control
        for the next test: it proves the setup CAN produce a lock and a buy."""
        await _world(pipeline, metar_f=93.9, wu_f=94.0)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.20, 0.22))   # NO costs 80c
        opp, _ = await _evaluate(pipeline)

        assert opp is not None
        assert opp.lock_state == "yes_impossible"
        assert opp.side == "NO" and opp.virtual_shares == 5

    @pytest.mark.asyncio
    async def test_a_max_that_only_touches_the_edge_is_not_bought(self, pipeline):
        """93.0°F is 0.5°F past 92.5 — inside measurement resolution. This is
        the shape of Houston 92-93°F at 94.0 and London 30°C at 88.0°F, both
        of which lost. It must not be priced as 98.5%."""
        await _world(pipeline, metar_f=92.9, wu_f=93.0)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.20, 0.22))
        opp, _ = await _evaluate(pipeline)

        assert opp is None or opp.virtual_shares is None
        assert pipeline.http.calls_to(pm.CLOB_BOOK), "the book must have been read"


@pytest.mark.parametrize("days_ahead", [0], indirect=True)
class TestEntryFloor:
    @pytest.mark.asyncio
    async def test_a_cheap_ask_alerts_but_does_not_buy(self, pipeline):
        await _world(pipeline, metar_f=93.9, wu_f=94.0)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.35, 0.37))   # NO costs 65c
        opp, _ = await _evaluate(pipeline)

        assert opp is not None, "the alert still fires"
        assert opp.virtual_shares is None and opp.virtual_status is None
        assert opp.signals["_entry_too_cheap"] is True
        assert opp.signals["_min_entry_cost"] == pytest.approx(0.70)

    @pytest.mark.asyncio
    async def test_the_same_bet_at_a_normal_ask_is_bought(self, pipeline):
        """The control: identical except the price, so only the floor differs."""
        await _world(pipeline, metar_f=93.9, wu_f=94.0)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.20, 0.22))   # NO costs 80c
        opp, _ = await _evaluate(pipeline)
        assert opp.virtual_shares == 5
        assert opp.signals["_entry_too_cheap"] is False

    @pytest.mark.asyncio
    async def test_zero_switches_the_floor_off(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.intraday_min_entry_cost", 0.0)
        await _world(pipeline, metar_f=93.9, wu_f=94.0)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.35, 0.37))
        opp, _ = await _evaluate(pipeline)
        assert opp.virtual_shares == 5


class TestColumnFitsEveryState:
    def test_every_lock_state_fits_the_column(self):
        """VARCHAR(20) could not hold "yes_impossible_unconfirmed" (26). On
        Postgres the insert failed and poisoned the session; SQLite ignores
        lengths, so no test saw it. Every value the estimator can emit must
        fit the column."""
        from app.intraday.estimator import SOFT_LOCK_STATES
        states = {"yes_impossible", "yes_locked", *SOFT_LOCK_STATES}
        limit = IntradayOpportunity.__table__.c.lock_state.type.length
        too_long = {s for s in states if len(s) > limit}
        assert not too_long, f"{too_long} exceed VARCHAR({limit})"

    def test_the_migration_widens_the_live_column(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "migrations" / "versions"
               / "024_widen_intraday_lock_state.py").read_text(encoding="utf-8")
        assert "String(32)" in src and '"lock_state"' in src
