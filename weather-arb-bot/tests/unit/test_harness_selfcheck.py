"""Proof that the test harness does what it claims.

A mock that silently stops intercepting is worse than no mock: tests keep
passing while covering nothing. These tests drive the *real* collectors and
the *real* send functions through the harness and assert on the result, so a
break in the plumbing fails here rather than leaking into every other file.
"""
from __future__ import annotations

import socket
from datetime import date, timedelta

import httpx
import pytest

from tests.mocks import weather_payloads as wx
from tests.mocks import polymarket_payloads as pm
from tests.mocks.db import FakeSession
from tests.mocks.http_router import UnmockedRequest


# ── Guarantee 1: the network is closed ────────────────────────────────────

class TestNetworkGuard:
    def test_raw_socket_to_the_internet_is_blocked(self):
        with pytest.raises(Exception) as exc:
            socket.create_connection(("api.open-meteo.com", 443), timeout=1)
        assert "Blocked outbound connection" in str(exc.value)

    def test_loopback_still_works(self):
        """Blocking everything would break pytest plugins and aiosqlite."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        client = socket.socket()
        client.connect(s.getsockname())     # must not raise
        client.close()
        s.close()

    @pytest.mark.asyncio
    async def test_unrouted_http_call_fails_loudly(self, http):
        """The failure mode that matters: a payload nobody wrote must never
        be invented. An unregistered endpoint raises."""
        from app.collectors.gfs_collector import GFSCollector
        with pytest.raises(UnmockedRequest):
            await GFSCollector()._get(wx.OPEN_METEO)


# ── Guarantee 2: every client construction site is intercepted ────────────

class TestHttpInterception:
    @pytest.mark.asyncio
    async def test_base_collector_path(self, weather_api, today):
        """The 8 collectors that go through `BaseCollector._get`."""
        from app.collectors.gfs_collector import GFSCollector
        got = await GFSCollector().collect(30.1975, -97.6664, "gfs", today)
        assert got["predicted_high_f"] == 95
        assert got["predicted_low_f"] == 74

    @pytest.mark.asyncio
    async def test_client_built_outside_base_collector(self, polymarket_api):
        """admin.py, jobs.py and polymarket_discovery build their own
        `httpx.AsyncClient`. Patching `_get` would have missed them."""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://gamma-api.polymarket.com/events", params={"slug": "x"}
            )
        assert r.status_code == 200
        assert r.json()[0]["slug"] == "highest-temperature-in-austin-on-july-28"

    @pytest.mark.asyncio
    async def test_caller_supplied_transport_cannot_opt_out(self, polymarket_api):
        """Passing an explicit transport must not escape the router."""
        real = httpx.AsyncHTTPTransport()
        async with httpx.AsyncClient(transport=real) as client:
            r = await client.get("https://clob.polymarket.com/midpoint?token_id=1")
        assert r.json() == {"mid": "0.6200"}

    @pytest.mark.asyncio
    async def test_router_records_query_params(self, weather_api, today):
        """Assertions about *what was asked for* — the model selected, the
        units requested — need the request, not just the response."""
        from app.collectors.icon_collector import IconCollector
        await IconCollector().collect(30.1975, -97.6664, today)
        assert weather_api.param(wx.OPEN_METEO, "temperature_unit") == "fahrenheit"
        assert "icon" in (weather_api.param(wx.OPEN_METEO, "models") or "")

    @pytest.mark.asyncio
    async def test_sequenced_responses_drive_the_retry_path(self, http, monkeypatch, today):
        """`BaseCollector._get` retries 429 with backoff. Without a sequence
        there is no way to test that it eventually succeeds."""
        import app.collectors.base as base
        monkeypatch.setattr(base.asyncio, "sleep", _no_sleep)
        http.add_sequence(wx.OPEN_METEO, [
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(200, json=wx.open_meteo_daily(today, 95, 74)),
        ])
        from app.collectors.gfs_collector import GFSCollector
        got = await GFSCollector().collect(30.1975, -97.6664, "gfs", today)
        assert got["predicted_high_f"] == 95
        assert http.count(wx.OPEN_METEO) == 2, "should have retried exactly once"

    @pytest.mark.asyncio
    async def test_deterministic_4xx_is_not_retried(self, http, monkeypatch, today):
        import app.collectors.base as base
        monkeypatch.setattr(base.asyncio, "sleep", _no_sleep)
        http.add(wx.OPEN_METEO, {"error": "bad key"}, status=401)
        from app.collectors.gfs_collector import GFSCollector
        assert await GFSCollector().collect(30.1975, -97.6664, "gfs", today) is None
        assert http.count(wx.OPEN_METEO) == 1


async def _no_sleep(_seconds):
    """Collapse the 2s/4s/8s backoff so retry tests run instantly."""
    return None


# ── Payloads match what the collectors actually parse ─────────────────────

class TestPayloadsAreRealistic:
    @pytest.mark.asyncio
    async def test_nws_two_hop_lookup(self, weather_api, today):
        """NWS needs /points first, then the gridpoint URL it hands back.
        A single-response mock would not catch a break in that chain."""
        from app.collectors.nws_collector import NWSCollector
        got = await NWSCollector().collect(30.1975, -97.6664, today)
        assert got["predicted_high_f"] == 95
        assert got["predicted_low_f"] == 74
        assert got["grid_id"] == "EWX"
        assert weather_api.count(wx.NWS_POINTS) == 1
        assert weather_api.count(wx.NWS_GRIDPOINT) == 1

    @pytest.mark.asyncio
    async def test_target_date_is_looked_up_not_assumed(self, weather_api, today):
        """Payloads carry neighbouring days with different temperatures, so a
        collector that took index 0 would return the wrong number."""
        from app.collectors.gfs_collector import GFSCollector
        tomorrow = today + timedelta(days=1)
        got = await GFSCollector().collect(30.1975, -97.6664, "gfs", today)
        assert got["forecast_date"] == str(today)
        assert got["predicted_high_f"] == 95
        assert str(tomorrow) != got["forecast_date"]

    @pytest.mark.asyncio
    async def test_missing_date_returns_none_without_raising(self, http, today):
        """The collector-miss shape: HTTP 200, nothing usable inside."""
        http.add(wx.OPEN_METEO, wx.open_meteo_missing_date(today))
        from app.collectors.gfs_collector import GFSCollector
        assert await GFSCollector().collect(30.1975, -97.6664, "gfs", today) is None

    @pytest.mark.asyncio
    async def test_ensemble_spread_survives_the_round_trip(self, http, today):
        """Ensemble spread feeds sigma, so the fixture must reproduce the
        requested distribution exactly, not approximately."""
        http.add(wx.OPEN_METEO_ENSEMBLE,
                 wx.open_meteo_ensemble(today, [90.0, 92.0, 94.0, 96.0, 98.0]))
        from app.collectors.gfs_collector import GFSCollector
        got = await GFSCollector().collect_ensemble(30.1975, -97.6664, today)
        assert got["ensemble_count"] == 5
        assert got["ensemble_highs"] == [90.0, 92.0, 94.0, 96.0, 98.0]

    @pytest.mark.asyncio
    async def test_metar_celsius_is_converted(self, weather_api):
        """aviationweather reports Celsius; the bot stores Fahrenheit."""
        from app.collectors.metar_collector import MetarCollector
        got = await MetarCollector().collect("KAUS")
        assert got["temperature_f"] == pytest.approx(88.0, abs=0.2)  # 31.1°C
        assert got["raw_metar"].startswith("KAUS")

    @pytest.mark.asyncio
    async def test_empty_metar_is_handled(self, http):
        http.add(wx.METAR, wx.metar_empty())
        from app.collectors.metar_collector import MetarCollector
        assert await MetarCollector().collect("KAUS") is None

    @pytest.mark.asyncio
    async def test_gamma_json_encoded_string_fields(self):
        """Gamma sends `outcomes` as a JSON *string*. Indexing it raw yields
        a character — a bug this codebase has already had to fix."""
        import json
        m = pm.sub_market()
        assert isinstance(m["outcomes"], str)
        assert json.loads(m["outcomes"]) == ["Yes", "No"]

    def test_outcome_order_can_be_no_first(self):
        """`yes_first=False` builds the ["No", "Yes"] ordering, so a test can
        prove the reader locates Yes by label rather than by index 0."""
        import json
        m = pm.sub_market(yes_first=False, won=True)
        labels = json.loads(m["outcomes"])
        prices = json.loads(m["outcomePrices"])
        assert labels == ["No", "Yes"]
        assert prices[labels.index("Yes")] == "1.0"

    @pytest.mark.asyncio
    async def test_book_summary_from_payload(self, polymarket_api):
        from app.collectors.polymarket_collector import PolymarketCollector
        got = await PolymarketCollector().get_book_summary("tok")
        assert got == {"bid": 0.61, "ask": 0.63, "spread": 0.02, "mid": 0.62}

    @pytest.mark.asyncio
    async def test_one_sided_book_is_rejected(self, http):
        http.add(pm.CLOB_BOOK, pm.book(bid=0.61, ask=None))
        from app.collectors.polymarket_collector import PolymarketCollector
        assert await PolymarketCollector().get_book_summary("tok") is None


# ── Guarantee 3: Telegram is faked, and the send path really runs ─────────

class TestTelegramFake:
    @pytest.mark.asyncio
    async def test_send_path_executes_and_is_recorded(self, fake_telegram):
        from app.bot.telegram_bot import Bot
        await Bot(token="x").send_message(chat_id=42, text="hello", parse_mode="Markdown")
        msg = fake_telegram.only()
        assert msg.chat_id == 42 and msg.parse_mode == "Markdown"

    def test_fixture_supplies_a_token(self, fake_telegram):
        """The trap: every send function returns early on an empty token, so
        a test without one passes while exercising nothing."""
        from app.config import settings
        assert settings.telegram_bot_token, (
            "fake_telegram must set a token or the send path never runs"
        )

    def test_token_is_empty_by_default(self):
        """And without the fixture the bot stays silent, so no test can
        accidentally depend on a token leaking in from the environment."""
        from app.config import settings
        assert settings.telegram_bot_token == ""

    @pytest.mark.asyncio
    async def test_send_failure_can_be_simulated(self, fake_telegram):
        """Alert delivery must never abort the analyzer, which needs a way to
        make sending fail on demand."""
        from app.bot.telegram_bot import Bot
        fake_telegram.fail_with = RuntimeError("telegram down")
        with pytest.raises(RuntimeError):
            await Bot(token="x").send_message(chat_id=1, text="hi")

    @pytest.mark.asyncio
    async def test_messages_do_not_leak_between_tests(self, fake_telegram):
        assert fake_telegram.sent == []


# ── Database stand-ins ────────────────────────────────────────────────────

class TestDbMocks:
    @pytest.mark.asyncio
    async def test_fake_session_records_writes(self):
        from app.models.forecast import Forecast
        db = FakeSession()
        db.add(Forecast(city_id=1, source="gfs", forecast_for_date=date.today(),
                        predicted_high_f=95))
        await db.commit()
        assert db.commits == 1
        assert db.added_of(Forecast)[0].predicted_high_f == 95

    @pytest.mark.asyncio
    async def test_queued_results_are_returned_in_order(self):
        db = FakeSession().queue(["a", "b"], [])
        assert (await db.execute("q1")).scalars().all() == ["a", "b"]
        assert (await db.execute("q2")).scalars().all() == []

    @pytest.mark.asyncio
    async def test_exhausted_queue_yields_no_rows(self):
        db = FakeSession()
        assert (await db.execute("anything")).scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_collector_write_through_fake_session(self, weather_api, today):
        """End to end with both mocks: HTTP in, ORM row out, no Postgres."""
        from app.collectors.gfs_collector import GFSCollector
        from app.models.forecast import Forecast
        db = FakeSession()
        await GFSCollector().collect_and_store(1, 30.1975, -97.6664, today, db, "gfs")
        rows = db.added_of(Forecast)
        assert len(rows) == 1
        assert rows[0].predicted_high_f == 95 and rows[0].source == "gfs"

    @pytest.mark.asyncio
    async def test_sqlite_db_runs_real_sql(self, sqlite_db):
        """JSONB and PG ARRAY are taught to SQLite, so the real models load."""
        from sqlalchemy import select
        from app.models.city import City
        from tests.fixtures.cities import DEFAULT
        sqlite_db.add(DEFAULT.as_row())
        await sqlite_db.commit()
        got = (await sqlite_db.execute(select(City))).scalars().all()
        assert [c.name for c in got] == ["Austin"]

    @pytest.mark.asyncio
    async def test_sqlite_db_round_trips_jsonb(self, sqlite_db):
        from sqlalchemy import select
        from app.models.forecast import Forecast
        sqlite_db.add(Forecast(city_id=1, source="gfs", forecast_for_date=date(2026, 7, 28),
                               predicted_high_f=95, raw_data={"ensemble_count": 31}))
        await sqlite_db.commit()
        row = (await sqlite_db.execute(select(Forecast))).scalars().one()
        assert row.raw_data == {"ensemble_count": 31}


# ── Fixtures track the application, not a copy of it ──────────────────────

class TestDataFixtures:
    def test_cities_are_generated_from_the_seed_list(self, cities):
        """If a city is added to `app.utils.seed.CITIES`, it appears here
        automatically — the fixtures cannot silently go stale."""
        from app.utils.seed import CITIES as SEED
        assert len(cities.CITIES) == len(SEED)
        assert [c.name for c in cities.CITIES] == [s[0] for s in SEED]

    def test_nyc_resolves_on_knyc_not_the_airport(self, cities):
        """Polymarket settles NYC on Central Park. Hard-coding KLGA anywhere
        is a resolution bug, so the fixture pins the real station."""
        assert cities.NYC.primary_icao == "KNYC"
        assert cities.NYC.reference_icao == "KLGA"

    def test_city_rows_build_against_the_real_model(self, austin):
        from app.models.city import City
        row = austin.as_row()
        assert isinstance(row, City)
        assert row.nws_lat == austin.lat and row.wunderground_url

    def test_scenario_sources_track_the_estimator(self, scenarios):
        from app.analyzers.probability_estimator import _DET_SOURCES
        assert set(scenarios.ALL_SOURCES) == {k for k, _, _ in _DET_SOURCES}
        assert set(scenarios.GLOBAL_SOURCES) | set(scenarios.CONUS_ONLY_SOURCES) \
            == set(scenarios.ALL_SOURCES)

    def test_sparse_scenario_reports_the_shrink_it_will_trigger(self, scenarios):
        """`signals_sparse(2)` is 3 global sources short of the baseline of 5,
        which is a 24pp pull toward 0.5."""
        sparse = scenarios.signals_sparse(2)
        assert scenarios.missing_global_count(sparse) == 3
        full = scenarios.signals_full()
        assert scenarios.missing_global_count(full) == 0

    def test_international_scenario_is_short_only_conus_sources(self, scenarios):
        """London legitimately has no HRRR/NWS — that must not read as a gap."""
        intl = scenarios.signals_international()
        assert scenarios.missing_global_count(intl) == 0
        assert all(intl[k] is None for k in scenarios.CONUS_ONLY_SOURCES)

    def test_disagreement_scenario_actually_disagrees(self, scenarios):
        vals = [
            s["predicted_high_f"]
            for k in scenarios.ALL_SOURCES
            if (s := scenarios.signals_disagreement(spread=8.0)[k])
        ]
        assert max(vals) - min(vals) == pytest.approx(8.0)

    def test_exit_scenarios_match_the_monitor_thresholds(self, scenarios):
        """The named exit situations are derived from the live constants, so
        retuning a threshold cannot leave a scenario silently non-triggering."""
        from app.analyzers.exit_monitor import (
            EXIT_CONFIDENCE_DROP_PP, EXIT_FORECAST_SHIFT_F,
            EXIT_CERTAINTY_FLOOR, EXIT_FORECAST_SHIFT_EXTREME_F,
        )
        dual = scenarios.EXIT_DUAL_TRIGGER
        drop_pp = (dual["entry_certainty"] - dual["fresh_certainty"]) * 100
        shift = abs(dual["entry_high_f"] - dual["fresh_high_f"])
        assert drop_pp >= EXIT_CONFIDENCE_DROP_PP and shift >= EXIT_FORECAST_SHIFT_F

        assert scenarios.EXIT_FLOOR_BREACH["fresh_certainty"] < EXIT_CERTAINTY_FLOOR
        assert abs(
            scenarios.EXIT_EXTREME_SHIFT["entry_high_f"]
            - scenarios.EXIT_EXTREME_SHIFT["fresh_high_f"]
        ) >= EXIT_FORECAST_SHIFT_EXTREME_F

        quiet = scenarios.EXIT_NO_TRIGGER
        assert (quiet["entry_certainty"] - quiet["fresh_certainty"]) * 100 < EXIT_CONFIDENCE_DROP_PP
        assert quiet["fresh_certainty"] >= EXIT_CERTAINTY_FLOOR
        assert abs(quiet["entry_high_f"] - quiet["fresh_high_f"]) < EXIT_FORECAST_SHIFT_EXTREME_F

    def test_buckets_use_the_fahrenheit_half_open_convention(self, scenarios):
        ladder = scenarios.buckets(center=95, count=3, width=2)
        assert ladder == [("93-94°F", 93, 94), ("95-96°F", 95, 96), ("97-98°F", 97, 98)]

    def test_lead_times_straddle_the_trading_horizon(self, scenarios):
        from app.config import settings
        days = [d for d, _ in scenarios.lead_times()]
        assert settings.max_days_ahead_for_alert in days
        assert max(days) > settings.max_days_ahead_for_alert
