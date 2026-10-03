"""The intraday peak guard, through the real detector.

World (from test_intraday_guards): Austin, market resolving today, bucket
91-92°F, the max 94°F already past it → a NO lock the detector buys. The
detector is called at 16:30 local. daily_peaks decides whether, on most of
Austin's days this month, the high had already come by then.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import app.peaks.models  # noqa: F401  — registers the table
from app.peaks.guard import share_passed
from app.peaks.models import DailyPeak
from tests.flow.test_intraday_guards import _evaluate, _world
from tests.mocks import polymarket_payloads as pm


async def _peaks(pipeline, peak_hour: float, n: int = 12, complete=True):
    month = date.today().month
    async with pipeline.session() as db:
        for i in range(n):
            d = date(2025, month, 1) + timedelta(days=i)     # same month, last year
            db.add(DailyPeak(
                city_id=pipeline.city.id, local_date=d, icao="KAUS", max_f=95.0,
                peak_hour=peak_hour, peak_last_hour=peak_hour, n_obs=24 if complete else 6,
                first_obs_hour=0.0, last_obs_hour=23.0 if complete else 15.0,
                max_gap_h=1.0, spike_dropped=0, computed_at=datetime.now(timezone.utc)))
        await db.commit()


async def _setup(pipeline, monkeypatch, *, peak_hour, enabled):
    monkeypatch.setattr("app.config.settings.intraday_peak_guard_enabled", enabled)
    await _world(pipeline, metar_f=93.9, wu_f=94.0)
    pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.20, 0.22))     # NO costs 80c
    await _peaks(pipeline, peak_hour)


@pytest.mark.parametrize("days_ahead", [0], indirect=True)
class TestThroughTheDetector:
    @pytest.mark.asyncio
    async def test_off_it_records_but_still_buys(self, pipeline, monkeypatch):
        """Austin's days peak at 18:00; at 16:30 none had. Off: the buy happens
        and the signal says the guard would have blocked it."""
        await _setup(pipeline, monkeypatch, peak_hour=18.0, enabled=False)
        opp, _ = await _evaluate(pipeline)
        assert opp is not None and opp.virtual_shares == 5
        assert opp.signals["_peak_guard_would_block"] is True
        assert opp.signals["_peak_passed_share"] == 0.0
        assert opp.signals["_peak_guard_enabled"] is False

    @pytest.mark.asyncio
    async def test_on_it_blocks_the_buy_not_the_alert(self, pipeline, monkeypatch):
        await _setup(pipeline, monkeypatch, peak_hour=18.0, enabled=True)
        opp, _ = await _evaluate(pipeline)
        assert opp is not None, "the alert still goes out"
        assert opp.virtual_shares is None and opp.signals["_create_virtual_buy"] is False

    @pytest.mark.asyncio
    async def test_on_but_past_the_usual_peak_it_does_not_block(self, pipeline, monkeypatch):
        await _setup(pipeline, monkeypatch, peak_hour=15.0, enabled=True)
        opp, _ = await _evaluate(pipeline)
        assert opp.virtual_shares == 5
        assert opp.signals["_peak_guard_would_block"] is False
        assert opp.signals["_peak_passed_share"] == 1.0

    @pytest.mark.asyncio
    async def test_without_enough_measured_days_it_never_blocks(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.intraday_peak_guard_enabled", True)
        await _world(pipeline, metar_f=93.9, wu_f=94.0)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.20, 0.22))
        await _peaks(pipeline, 18.0, n=5)
        opp, _ = await _evaluate(pipeline)
        assert opp.virtual_shares == 5
        assert opp.signals["_peak_passed_share"] is None and opp.signals["_peak_days"] == 5

    @pytest.mark.asyncio
    async def test_a_failing_peak_read_never_blocks(self, pipeline, monkeypatch):
        import app.peaks.guard as guard

        async def boom(*a, **k):
            raise RuntimeError("table missing")
        monkeypatch.setattr(guard, "_peak_hours", boom)
        monkeypatch.setattr("app.config.settings.intraday_peak_guard_enabled", True)
        await _world(pipeline, metar_f=93.9, wu_f=94.0)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.20, 0.22))
        opp, _ = await _evaluate(pipeline)
        assert opp.virtual_shares == 5 and opp.signals["_peak_passed_share"] is None


class TestShare:
    @pytest.mark.asyncio
    async def test_share_of_complete_days_in_the_month(self, sqlite_db):
        month = 7
        for i, ph in enumerate([13.0] * 6 + [17.0] * 6):
            sqlite_db.add(DailyPeak(
                city_id=1, local_date=date(2026, month, 1 + i), icao="KAUS", max_f=90,
                peak_hour=ph, peak_last_hour=ph, n_obs=24, first_obs_hour=0, last_obs_hour=23,
                max_gap_h=1, spike_dropped=0, computed_at=datetime.now(timezone.utc)))
        # an incomplete day (afternoon only) must not count
        sqlite_db.add(DailyPeak(
            city_id=1, local_date=date(2026, month, 20), icao="KAUS", max_f=90,
            peak_hour=20.0, peak_last_hour=20.0, n_obs=5, first_obs_hour=15, last_obs_hour=21,
            max_gap_h=1, spike_dropped=0, computed_at=datetime.now(timezone.utc)))
        await sqlite_db.commit()
        assert await share_passed(sqlite_db, 1, month, 15.0) == (0.5, 12)
        assert await share_passed(sqlite_db, 1, month, 17.0) == (1.0, 12)
        assert await share_passed(sqlite_db, 1, 8, 15.0) == (None, 0)

    @pytest.mark.asyncio
    async def test_it_is_read_once_a_day(self, sqlite_db):
        import app.peaks.guard as guard
        await share_passed(sqlite_db, 1, 7, 15.0, today=date(2026, 7, 1))
        calls = []
        real = sqlite_db.execute

        async def counting(*a, **k):
            calls.append(1)
            return await real(*a, **k)
        sqlite_db.execute = counting
        await share_passed(sqlite_db, 1, 7, 16.0, today=date(2026, 7, 1))
        assert calls == [], "same day: served from the cache"
        await share_passed(sqlite_db, 1, 7, 16.0, today=date(2026, 7, 2))
        assert calls == [1], "a new day reads again"
        assert 1 in guard._CACHE


class TestReport:
    @pytest.mark.asyncio
    async def test_it_splits_by_what_the_guard_would_have_done(self, sqlite_db):
        from app.api.admin import admin_peak_guard_report
        from app.models.intraday import IntradayOpportunity
        from app.models.market import Market, MarketOutcome
        sqlite_db.add(Market(id=1, city_id=1, external_id="m", question="q", event_date=date.today()))
        sqlite_db.add(MarketOutcome(id=1, market_id=1, bucket_label="b", bucket_unit="F"))
        now = datetime.now(timezone.utc)

        def opp(i, block, share, vstatus, pnl, outcome):
            sig = {"_peak_guard_would_block": block, "_peak_passed_share": share}
            return IntradayOpportunity(id=i, outcome_id=1, detected_at=now, side="NO",
                                       market_price=0.2, estimated_true_prob=0.05, edge=0.1,
                                       confidence_score=95, signals=sig,
                                       virtual_status=vstatus, virtual_pnl=pnl, outcome=outcome)
        sqlite_db.add_all([
            opp(1, True, 0.2, "loss", -4.0, "LOSS"),
            opp(2, True, 0.3, "win", 1.0, "WIN"),
            opp(3, False, 0.8, "win", 1.0, "WIN"),
            opp(4, False, None, "win", 1.0, "WIN"),
            IntradayOpportunity(id=5, outcome_id=1, detected_at=now, side="NO", market_price=0.2,
                                estimated_true_prob=0.05, edge=0.1, confidence_score=95,
                                signals={"old": True}, virtual_status="win", virtual_pnl=1.0),
        ])
        await sqlite_db.commit()
        out = await admin_peak_guard_report("t", sqlite_db, days=30)
        assert out["enabled"] is False
        assert out["would_block"] == {"opportunities": 2, "alerts_settled": 2, "alert_win_rate": 0.5,
                                      "buys_settled": 2, "buy_win_rate": 0.5, "buy_pnl": -3.0}
        assert out["would_allow"]["opportunities"] == 1 and out["would_allow"]["buy_pnl"] == 1.0
        assert out["no_peak_data"]["opportunities"] == 1, "a signal without peak data"


class TestSettings:
    @pytest.mark.asyncio
    async def test_the_switch_and_threshold_persist(self, monkeypatch):
        from app.api.admin import SettingsIn, admin_set_settings
        from app.config import settings
        from app.utils.settings_store import PERSISTABLE_KEYS
        from tests.flow.test_open_meteo_batch import _DB
        monkeypatch.setattr(settings, "intraday_peak_guard_enabled", False)
        await admin_set_settings(SettingsIn(intraday_peak_guard_enabled=True,
                                            intraday_peak_guard_min_passed=0.4), "t", _DB())
        assert settings.intraday_peak_guard_enabled is True
        assert settings.intraday_peak_guard_min_passed == 0.4
        assert {"intraday_peak_guard_enabled", "intraday_peak_guard_min_passed"} <= PERSISTABLE_KEYS

    def test_it_is_off_by_default(self):
        from app.config import Settings
        assert Settings.model_fields["intraday_peak_guard_enabled"].default is False
