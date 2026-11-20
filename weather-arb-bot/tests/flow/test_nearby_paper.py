"""The nearby-station paper test end to end: stored METARs -> signal -> live
book (mocked CLOB) -> recorded paper trade -> Telegram (FakeBot) -> follow-up
mids -> the report. Nothing is bought."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import app.nearby.paper as paper
from app.models.alert import TelegramUser
from app.models.market import Market, MarketOutcome
from app.models.metar import MetarObservation
from app.nearby.models import NearbySignal
from app.nearby.stations import neighbours
from app.peaks.models import DailyPeak
from tests.fixtures import cities as city_fixtures
from tests.mocks import polymarket_payloads as pm
from tests.mocks import sqlite_compat

AUSTIN = city_fixtures.get("Austin")
TZ = ZoneInfo(AUSTIN.tz)
T = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)          # 14:00 in Austin
DAY = T.astimezone(TZ).date()
NB = neighbours("KAUS")


async def _world(monkeypatch, *, falling=4, target_high=70.2, target_now=69.8, users=True,
                 peak_hour=13.0, peak_month=None):
    from app.database import Base
    import app.models  # noqa: F401
    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    async with maker() as db:
        db.add(AUSTIN.as_row())
        await db.flush()
        db.add(Market(id=1, city_id=AUSTIN.id, external_id="m", question="q", event_date=DAY))
        await db.flush()
        for oid, (lo, hi) in enumerate(((68, 69), (70, 71), (72, 73)), start=10):
            db.add(MarketOutcome(id=oid, market_id=1, bucket_label=f"{lo}-{hi}°F", bucket_min=lo,
                                 bucket_max=hi, bucket_unit="F", token_id=f"tok{lo}"))
        start = datetime.combine(DAY, time(10), tzinfo=TZ).astimezone(timezone.utc)
        def series(icao, temps):
            for i, t in enumerate(temps):
                db.add(MetarObservation(icao=icao, observed_at=start + timedelta(hours=i), temperature_f=t))
        series("KAUS", [64, 67, 69, target_high, target_now])
        for i, icao in enumerate(NB):
            series(icao, [63, 66, 68, 69, 67.5] if i < falling else [63, 65, 67, 68, 69])
        # Twelve complete past days whose high came at `peak_hour` — what the
        # peak guard reads. In the event's month unless told otherwise.
        month = peak_month or DAY.month
        for k in range(12):
            d = DAY.replace(month=month, day=1) + timedelta(days=k)
            if d >= DAY:
                d = d.replace(year=d.year - 1)
            db.add(DailyPeak(city_id=AUSTIN.id, local_date=d, icao="KAUS", max_f=70.0, peak_hour=peak_hour,
                             peak_last_hour=peak_hour, n_obs=24, first_obs_hour=0.5, last_obs_hour=23.5,
                             max_gap_h=1.0, spike_dropped=0, computed_at=T))
        if users:
            db.add(TelegramUser(chat_id=111))
        await db.commit()
    return engine, maker


async def _run(maker, http, at=T):
    async with maker() as db:
        return await paper.run_paper(db, at)


async def _signals(maker):
    async with maker() as db:
        return (await db.execute(select(NearbySignal))).scalars().all()


@pytest.fixture
async def world(monkeypatch):
    engine, maker = await _world(monkeypatch)
    yield maker
    await engine.dispose()


class TestTheSignal:
    @pytest.mark.asyncio
    async def test_a_signal_is_recorded_at_the_live_ask_and_announced(self, http, world, fake_telegram):
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.70, ask=0.74))
        stats = await _run(world, http)
        (s,) = await _signals(world)
        assert stats["recorded"] == 1 and stats["alerted"] == 1
        assert s.outcome_id == 11, "70.2°F rounds to 70: the 70-71°F bucket holds the high"
        assert (s.n_falling, s.n_nearby) == (4, 5)
        assert (s.bid, s.ask, s.ask_size) == (0.70, 0.74, 100.0)
        assert http.param(pm.CLOB_BOOK, "token_id") == "tok70"
        msg = fake_telegram.only()
        first = msg.text.splitlines()[0]
        assert "YES 70-71°F" in first and "Austin" in first, "the bet comes first"
        assert "Ask <b>74.0¢</b>" in msg.text.splitlines()[1] and "worth ~85¢" in msg.text
        assert 'href="https://polymarket.com/event/m"' in msg.text, "one tap to the market"
        assert "Past the usual peak: on 100%" in msg.text
        assert "4 of 5" in msg.text and "°C" not in msg.text, "a °F market speaks °F"
        assert "Paper only" in msg.text and msg.chat_id == 111
        assert s.tradable is True

    @pytest.mark.asyncio
    async def test_only_the_first_signal_per_market(self, http, world, fake_telegram):
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.70, ask=0.74))
        await _run(world, http)
        await _run(world, http, T + timedelta(minutes=5))
        assert len(await _signals(world)) == 1 and len(fake_telegram.sent) == 1

    @pytest.mark.asyncio
    async def test_too_few_neighbours_falling_records_nothing(self, http, monkeypatch, fake_telegram):
        engine, maker = await _world(monkeypatch, falling=2)            # 2 of 5 = 40%
        try:
            stats = await _run(maker, http)
            assert stats["fired"] == 0 and await _signals(maker) == []
            assert http.count() == 0, "no book read when nothing fires"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_out_of_band_waits_and_fires_later_in_band(self, http, world, fake_telegram):
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.90, ask=0.92), once=True)
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.70, ask=0.74))
        first = await _run(world, http)
        assert first["fired"] == 1 and first["recorded"] == 0
        await _run(world, http, T + timedelta(minutes=5))
        (s,) = await _signals(world)
        assert s.ask == 0.74

    @pytest.mark.asyncio
    async def test_a_wide_book_is_recorded_but_not_announced_or_scored(self, http, world, fake_telegram):
        """Munich, first day live: mid 84c sat inside the band, but the ask was
        99.5c on a 30c spread — there was no trade to make."""
        from app.api.admin import admin_nearby_report
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.692, ask=0.995))
        stats = await _run(world, http)
        (s,) = await _signals(world)
        assert stats["recorded"] == 1 and stats["not_tradable"] == 1 and stats["alerted"] == 0
        assert s.tradable is False and fake_telegram.sent == []
        async with world() as db:
            (await db.get(MarketOutcome, 11)).won = True
            await db.commit()
            rep = await admin_nearby_report("t", db, days=30, from_date=None, to_date=None, city_id=None)
        assert rep["signals"] == 1 and rep["tradable"] == 0 and rep["settled"] == 0
        assert rep["pnl_per_share_at_ask"] is None

    @pytest.mark.asyncio
    async def test_a_wide_book_does_not_lock_the_market_for_the_day(self, http, world, fake_telegram):
        """Live since #136: the first signal met a 30c spread, was recorded as not
        tradable, and the market was never looked at again — no alert at all,
        even once the book tightened five minutes later."""
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.692, ask=0.995), once=True)
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.62, ask=0.65))
        await _run(world, http)
        assert fake_telegram.sent == []
        stats = await _run(world, http, T + timedelta(minutes=5))
        (s,) = await _signals(world)
        assert stats["alerted"] == 1 and len(fake_telegram.sent) == 1
        assert s.tradable is True and s.ask == 0.65
        assert s.first_seen_at.replace(tzinfo=timezone.utc) == T, "when it first fired is kept"
        assert s.signaled_at.replace(tzinfo=timezone.utc) == T + timedelta(minutes=5)
        await _run(world, http, T + timedelta(minutes=10))
        assert len(fake_telegram.sent) == 1, "a tradable signal still closes the market"

    @pytest.mark.asyncio
    async def test_before_the_usual_peak_nothing_fires(self, http, monkeypatch, fake_telegram):
        """San Francisco and Los Angeles, live: the neighbours were falling but
        the settlement station was still climbing — both lost."""
        engine, maker = await _world(monkeypatch, peak_hour=16.0)        # usual peak 16:00, now 14:00
        try:
            stats = await _run(maker, http)
            assert stats["fired"] == 0 and stats["before_peak"] == 1 and http.count() == 0
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_early_in_a_month_the_previous_month_is_used(self, http, monkeypatch, fake_telegram):
        engine, maker = await _world(monkeypatch, peak_month=DAY.month - 1)
        try:
            http.add(pm.CLOB_BOOK, json=pm.book(bid=0.70, ask=0.74))
            assert (await _run(maker, http))["recorded"] == 1
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_wide_spread_is_fine_when_the_ask_is_still_worth_it(self, http, world, fake_telegram):
        """mid 75c has been worth ~85c: an 83c ask on a 16c spread is a buy."""
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.67, ask=0.83))
        await _run(world, http)
        (s,) = await _signals(world)
        assert s.tradable is True and len(fake_telegram.sent) == 1

    @pytest.mark.asyncio
    async def test_alerts_can_be_switched_off(self, http, world, fake_telegram, monkeypatch):
        monkeypatch.setattr("app.config.settings.nearby_alerts_enabled", False)
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.70, ask=0.74))
        await _run(world, http)
        (s,) = await _signals(world)
        assert fake_telegram.sent == [] and s.alerted is False

    @pytest.mark.asyncio
    async def test_outside_the_window_nothing_happens(self, http, world, fake_telegram):
        stats = await _run(world, http, T.replace(hour=4))               # 22:00 the day before in Austin
        assert stats["checked"] == 0 and http.count() == 0

    @pytest.mark.asyncio
    async def test_a_stale_settlement_station_never_fires(self, http, world, fake_telegram):
        stats = await _run(world, http, T + timedelta(hours=3))          # last reading 3h old
        assert stats["fired"] == 0

    @pytest.mark.asyncio
    async def test_a_failure_is_contained(self, http, world, fake_telegram, no_backoff):
        http.add(pm.CLOB_BOOK, status=500, json={})
        stats = await _run(world, http)
        assert "error" not in stats and stats["recorded"] == 0


class TestFollowUpsAndReport:
    @pytest.mark.asyncio
    async def test_mids_after_10_and_30_minutes_and_the_settled_result(self, http, world, fake_telegram):
        from app.api.admin import admin_nearby_report
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.70, ask=0.74), once=True)
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.66, ask=0.68), once=True)
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.72, ask=0.76))
        await _run(world, http)
        await _run(world, http, T + timedelta(minutes=5))                # too early for anything
        await _run(world, http, T + timedelta(minutes=10))
        await _run(world, http, T + timedelta(minutes=30))
        (s,) = await _signals(world)
        assert s.mid_10 == pytest.approx(0.67) and s.mid_30 == pytest.approx(0.74)   # entry mid was 0.72
        async with world() as db:
            (await db.get(MarketOutcome, 11)).won = True
            await db.commit()
            rep = await admin_nearby_report("t", db, days=30, from_date=None, to_date=None, city_id=None)
        assert rep["signals"] == rep["settled"] == rep["wins"] == 1
        (r,) = rep["rows"]
        assert r["pnl_per_share"] == pytest.approx(0.26) and r["nearby"] == "4/5"
        assert r["move_10"] == pytest.approx(-0.05) and r["move_30"] == pytest.approx(0.02)
        assert rep["pnl_per_share_at_ask"] == pytest.approx(0.26)
        assert r["link"] == "https://polymarket.com/event/m"


class TestReportFilters:
    @pytest.mark.asyncio
    async def test_dates_city_and_the_per_row_fields(self, http, world, fake_telegram):
        from app.api.admin import admin_nearby_report
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.692, ask=0.995))
        await _run(world, http)
        async with world() as db:
            rep = lambda **kw: admin_nearby_report("t", db, days=30, **{"from_date": None, "to_date": None, "city_id": None, **kw})
            r = await rep()
            (row,) = r["rows"]
            assert row["local_time"] == "14:00" and row["city_id"] == AUSTIN.id
            assert row["spread"] == pytest.approx(0.303) and row["result"] == "open" and row["tradable"] is False
            assert (await rep(city_id=AUSTIN.id + 1))["signals"] == 0
            assert (await rep(from_date=DAY + timedelta(days=1)))["signals"] == 0
            assert (await rep(from_date=DAY, to_date=DAY))["signals"] == 1
            (await db.get(MarketOutcome, 11)).won = False
            await db.commit()
            r = await rep()
        assert r["rows"][0]["result"] == "lost"
        assert r["rows"][0]["pnl_per_share"] == pytest.approx(-0.995), "what it WOULD have made, shown faded"
        assert r["settled"] == 0, "not buyable: never scored"


class TestByCity:
    @pytest.mark.asyncio
    async def test_the_city_summary_and_its_stations(self, http, world, fake_telegram):
        from app.api.admin import admin_nearby_report, admin_nearby_stations
        from app.nearby.stations import NEARBY, NEARBY_HISTORY
        http.add(pm.CLOB_BOOK, json=pm.book(bid=0.70, ask=0.74))
        await _run(world, http)
        async with world() as db:
            (await db.get(MarketOutcome, 11)).won = True
            await db.commit()
            rep = await admin_nearby_report("t", db, days=30, from_date=None, to_date=None, city_id=None)
            st = await admin_nearby_stations("t", db, days=90, city_id=None)
            other = await admin_nearby_stations("t", db, days=90, city_id=AUSTIN.id + 1)
        (c,) = rep["by_city"]
        assert c["city"] == "Austin" and (c["signals"], c["buyable"], c["settled"], c["wins"]) == (1, 1, 1, 1)
        assert c["pnl_per_share"] == pytest.approx(0.26) and c["enough"] is False
        rows = {r["station"]: r for r in st["rows"]}
        assert set(rows) == {s for s, _, _ in NEARBY["KAUS"]}
        falling = NB[:4]                                     # the fixture's four falling neighbours
        for icao in falling:
            assert (rows[icao]["past_high"], rows[icao]["settled"], rows[icao]["won"]) == (1, 1, 1)
        assert rows[NB[4]]["past_high"] == 0, "the rising one was not past its high"
        r0 = rows[NB[0]]
        km, bearing = next((k, b) for s_, k, b in NEARBY["KAUS"] if s_ == NB[0])
        assert r0["km"] == km and r0["bearing"] == bearing and r0["direction"] in ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        assert (r0["hist_lift_jul_sep"], r0["hist_lift_oct_nov"]) == NEARBY_HISTORY.get(("KAUS", NB[0]), (None, None))
        assert other["rows"] == [], "the city filter applies"

    def test_the_history_table_only_names_listed_stations(self):
        from app.nearby.stations import NEARBY, NEARBY_HISTORY
        for (target, icao), (a, b) in NEARBY_HISTORY.items():
            assert icao in {s for s, _, _ in NEARBY[target]}
            assert a is None or -50 < a < 50


class TestSettings:
    @pytest.mark.asyncio
    async def test_the_switches_persist(self, monkeypatch):
        from app.api.admin import SettingsIn, admin_set_settings
        from app.config import settings
        from app.utils.settings_store import PERSISTABLE_KEYS
        from tests.flow.test_open_meteo_batch import _DB
        for k in ("nws_obs_enabled", "nearby_obs_enabled", "nearby_paper_enabled", "nearby_alerts_enabled"):
            monkeypatch.setattr(settings, k, True)
            assert k in PERSISTABLE_KEYS
        await admin_set_settings(SettingsIn(nws_obs_enabled=False, nearby_paper_enabled=False), "t", _DB())
        assert settings.nws_obs_enabled is False and settings.nearby_paper_enabled is False
        assert settings.nearby_obs_enabled is True

    @pytest.mark.asyncio
    async def test_the_job_skips_the_paper_step_when_off(self, http, monkeypatch):
        import app.nearby.job as job
        monkeypatch.setattr("app.config.settings.nearby_paper_enabled", False)
        engine, maker = await _world(monkeypatch)
        try:
            monkeypatch.setattr(job, "AsyncSessionLocal", maker)
            http.add("aviationweather.gov/api/data/metar", json=[])
            await job.job_record_nearby()
            assert "paper" not in job.LAST_RUN
        finally:
            await engine.dispose()


class TestBoundary:
    def test_trading_code_does_not_depend_on_it(self):
        """The nearby test trades on paper. Only the scheduler and the admin
        screen may know it exists; if trading imported it, a bug here could
        move a real trade."""
        from pathlib import Path
        app_dir = Path(__file__).resolve().parents[2] / "app"
        importers = {
            str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py")
            if "app.nearby" in p.read_text(encoding="utf-8")
            and not str(p.relative_to(app_dir)).startswith("nearby")
        }
        assert importers <= {"main.py", "api/admin.py"}, importers
