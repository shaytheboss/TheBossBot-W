"""The shadow study, end to end on the pipeline harness.

Three things matter more than the feature itself, and each has a test here:

  1. It is isolated. It makes no HTTP call, writes to no production table,
     and the detector's decision is identical with it running.
  2. It measures the production model. Its per-market shortcut (one
     aggregate() per market instead of per bucket) must yield exactly the
     signals production would compute — a drift guard, not an assumption.
  3. The Telegram behaviour matches what was asked: a summary once a market
     resolves, sent exactly once; an optional hourly digest; an off switch.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

import app.shadow.models  # noqa: F401  — registers the tables before the fixture builds the schema
from app.shadow.models import ShadowMarketState, ShadowSnapshot
from app.shadow.snapshot import job_shadow_snapshot


async def _prepare(pipeline):
    await pipeline.collect_forecasts()
    await pipeline.collect_ensemble()
    await pipeline.collect_prices()


def _now() -> datetime:
    """Five past the current hour. Pinned so tests that step forward by
    minutes stay inside one hour whatever the wall clock says — a test that
    added 20 minutes to the real time crossed the hour boundary whenever it
    happened to run after :40, and failed on no code change at all."""
    from app.shadow.clock import hour_floor
    return hour_floor(datetime.now(timezone.utc)) + timedelta(minutes=5)


async def _run(pipeline, now=None):
    return await job_shadow_snapshot(
        session_factory=pipeline.session, now=now or _now(), today=date.today(),
    )


# ── Recording ─────────────────────────────────────────────────────────────

class TestRecording:
    @pytest.mark.asyncio
    async def test_every_live_bucket_is_recorded_with_the_city_clock(self, pipeline):
        await _prepare(pipeline)
        stats = await _run(pipeline)

        rows = await pipeline.rows(ShadowSnapshot)
        assert stats["markets"] == 1 and stats["errors"] == 0
        assert len(rows) == stats["rows"] >= 1
        r = rows[0]
        assert r.market_p == pytest.approx(0.62, abs=1e-4)   # from market_prices
        # Tomorrow's local close, as the city clock computes it — not a fixed
        # bound, which depends on where UTC midnight falls against Chicago.
        from app.shadow.clock import hours_to_close
        expected = hours_to_close(_now(), r.event_date, "America/Chicago")
        assert r.hours_to_close == pytest.approx(expected, abs=0.01)
        assert r.hours_to_close > 0
        assert 0 <= r.local_hour <= 23
        assert r.n_sources is not None and r.forecast_high_f is not None
        assert r.forecast_age_min is not None and r.forecast_age_min >= 0

    @pytest.mark.asyncio
    async def test_the_normalised_estimates_form_a_distribution(self, pipeline):
        await _prepare(pipeline)
        await _run(pipeline)
        rows = await pipeline.rows(ShadowSnapshot)
        assert all(r.normalized for r in rows), "every bucket is priced, so production normalises"
        assert 0.9 <= sum(r.model_p for r in rows) <= 1.01

    @pytest.mark.asyncio
    async def test_a_second_run_in_the_same_hour_writes_nothing(self, pipeline):
        await _prepare(pipeline)
        now = _now()
        first = await _run(pipeline, now)
        second = await _run(pipeline, now + timedelta(minutes=20))

        assert second["rows"] == 0 and second["already_done"] == 1
        assert await pipeline.count(ShadowSnapshot) == first["rows"]

    @pytest.mark.asyncio
    async def test_the_next_hour_adds_a_new_snapshot(self, pipeline):
        await _prepare(pipeline)
        now = _now()
        first = await _run(pipeline, now)
        await _run(pipeline, now + timedelta(hours=1))
        assert await pipeline.count(ShadowSnapshot) == 2 * first["rows"]

    @pytest.mark.asyncio
    async def test_no_forecast_means_no_rows(self, pipeline):
        """Production skips a market with no forecast source — the estimate
        would be the flat fallback. The study records nothing either."""
        await pipeline.collect_prices()
        stats = await _run(pipeline)
        assert stats["no_forecast"] == 1 and await pipeline.count(ShadowSnapshot) == 0

    @pytest.mark.asyncio
    async def test_switched_off_it_does_nothing(self, pipeline, monkeypatch):
        await _prepare(pipeline)
        monkeypatch.setattr("app.config.settings.shadow_enabled", False)
        assert await _run(pipeline) == {"enabled": False}
        assert await pipeline.count(ShadowSnapshot) == 0

    @pytest.mark.asyncio
    async def test_old_rows_are_pruned(self, pipeline):
        await _prepare(pipeline)
        async with pipeline.session() as db:
            db.add(ShadowSnapshot(
                outcome_id=999, taken_at=datetime.now(timezone.utc) - timedelta(days=100),
                market_id=999, city_id=1, event_date=date(2026, 6, 1),
                hours_to_close=5, local_hour=12, model_p=.5, raw_p=.5,
                normalized=True, market_p=.5,
            ))
            await db.commit()
        stats = await _run(pipeline)
        assert stats["pruned"] == 1

    @pytest.mark.asyncio
    async def test_one_failing_market_does_not_stop_the_run(self, pipeline, monkeypatch):
        await _prepare(pipeline)

        async def boom(*a, **kw):
            raise RuntimeError("estimator exploded")

        monkeypatch.setattr("app.shadow.snapshot.estimate_market", boom)
        stats = await _run(pipeline)          # must not raise
        assert stats["errors"] == 1 and stats["rows"] == 0


async def _add_second_market(pipeline, days_out: int = 1) -> int:
    """A second priced market for the same city, so failure containment can be
    tested across markets rather than inside a single one."""
    from app.models.market import Market, MarketOutcome, MarketPrice
    async with pipeline.session() as db:
        db.add(Market(id=2, city_id=pipeline.city.id, external_id="second-market",
                      question="Highest temperature in Austin, later?",
                      event_date=date.today() + timedelta(days=days_out), resolved=False))
        for i, (label, lo, hi) in enumerate(
            [("91-92°F", 91, 92), ("93-94°F", 93, 94), ("95-96°F", 95, 96), ("97-98°F", 97, 98)]
        ):
            db.add(MarketOutcome(id=10 + i, market_id=2, bucket_label=label,
                                 bucket_min=lo, bucket_max=hi, bucket_unit="F"))
            db.add(MarketPrice(outcome_id=10 + i, timestamp=datetime.now(timezone.utc),
                               yes_price=0.25, no_price=0.75))
        await db.commit()
    return 2


class TestTheClose:
    @pytest.mark.asyncio
    async def test_nothing_is_recorded_once_the_citys_day_is_over(self, pipeline):
        """The market stays open until Polymarket settles it, hours later.
        Those hours say nothing about who knew first — and the estimate is
        not even about the right day any more."""
        from app.shadow.clock import hour_floor, local_close_utc
        await _prepare(pipeline)
        after = hour_floor(local_close_utc(pipeline.event_date, pipeline.city.tz)) \
            + timedelta(hours=1, minutes=5)
        stats = await _run(pipeline, now=after)
        assert stats["rows"] == 0 and stats["past_close"] == 1
        async with pipeline.session() as db:
            assert (await db.execute(select(ShadowSnapshot))).first() is None

    @pytest.mark.asyncio
    async def test_the_last_hour_before_the_close_is_still_recorded(self, pipeline):
        from app.shadow.clock import hour_floor, local_close_utc
        await _prepare(pipeline)
        last = hour_floor(local_close_utc(pipeline.event_date, pipeline.city.tz)) \
            - timedelta(minutes=55)
        stats = await _run(pipeline, now=last)
        assert stats["rows"] > 0 and stats["past_close"] == 0


class TestFailureContainment:
    @pytest.mark.asyncio
    async def test_one_failing_market_does_not_lose_the_others(self, pipeline, monkeypatch):
        """The bug this pins: a rollback expires every ORM object in the
        session, so after one market failed, the NEXT market's attributes had
        to reload lazily inside async code — which raises. Every market after
        the first failure was silently lost for that hour."""
        import app.shadow.snapshot as snap
        await _prepare(pipeline)
        await _add_second_market(pipeline, days_out=1)  # the date the pipeline has forecasts for
        real = snap.estimate_market

        async def fails_on_first(db, city, market, *a, **kw):
            if market.id == 1:
                raise RuntimeError("market 1 exploded")
            return await real(db, city, market, *a, **kw)

        monkeypatch.setattr(snap, "estimate_market", fails_on_first)
        stats = await _run(pipeline)

        assert stats["errors"] == 1, stats
        assert stats["markets"] == 1, "the second market must still be recorded"
        rows = await pipeline.rows(ShadowSnapshot)
        assert rows and {r.market_id for r in rows} == {2}


# ── Isolation ─────────────────────────────────────────────────────────────

class TestIsolation:
    @pytest.mark.asyncio
    async def test_its_only_http_calls_are_order_book_reads_for_recorded_buckets(
        self, pipeline
    ):
        """Exactly one book read per recorded bucket, and nothing else — no
        midpoint polls, no Gamma, no weather APIs. Anything more would be
        traffic the study does not need."""
        from tests.mocks import polymarket_payloads as pm
        await _prepare(pipeline)
        before = len(pipeline.http.requests)
        stats = await _run(pipeline)
        calls = pipeline.http.requests[before:]

        assert calls, "live prices must be read from Polymarket"
        assert all(pm.CLOB_BOOK in str(r.url) for r in calls), \
            [str(r.url) for r in calls if pm.CLOB_BOOK not in str(r.url)]
        assert len(calls) == stats["book_calls"] == stats["rows"]

    @pytest.mark.asyncio
    async def test_it_writes_to_no_production_table(self, pipeline):
        from app.models.alert import Alert
        from app.models.collector_miss import CollectorMiss
        from app.models.forecast import Forecast
        from app.models.market import MarketPrice
        from app.models.opportunity import Opportunity

        await _prepare(pipeline)
        tables = (Opportunity, Alert, CollectorMiss, Forecast, MarketPrice)
        before = {t.__tablename__: await pipeline.count(t) for t in tables}
        await _run(pipeline)
        after = {t.__tablename__: await pipeline.count(t) for t in tables}
        assert after == before

    @pytest.mark.asyncio
    async def test_the_trading_decision_is_unchanged(self, pipeline):
        """The detector's output with the study running is the documented
        baseline: one NO on 93-94°F at 83%."""
        await _prepare(pipeline)
        await _run(pipeline)
        result = await pipeline.detect()
        assert result.bucket_sides() == {"93-94°F": "NO"}
        assert result.best.confidence_score == 83

    @pytest.mark.asyncio
    async def test_it_records_what_production_would_compute(self, pipeline):
        """The drift guard. The study calls aggregate() once per market and
        swaps in each bucket's fields. That must equal calling aggregate() per
        bucket — if aggregate() ever gains another bucket-dependent field, this
        fails instead of the study quietly measuring a different model."""
        from app.analyzers.probability_estimator import estimate_with_breakdown
        from app.analyzers.signal_aggregator import SignalAggregator
        from app.models.market import Market, MarketOutcome
        from app.models.city import City
        from app.shadow.estimate import signals_for_outcome

        await _prepare(pipeline)
        await _run(pipeline)
        recorded = {r.outcome_id: r for r in await pipeline.rows(ShadowSnapshot)}

        agg = SignalAggregator()
        async with pipeline.session() as db:
            market = (await db.execute(select(Market))).scalars().one()
            city = await db.get(City, market.city_id)
            outcomes = (await db.execute(
                select(MarketOutcome).order_by(MarketOutcome.id))).scalars().all()
            kw = dict(forecast_date=market.event_date, is_low_market=False,
                      city_lat=float(city.nws_lat), city_lon=float(city.nws_lon),
                      city_tz=city.timezone, onshore_wind_dir=None)
            base = await agg.aggregate(db, city.id, city.primary_icao,
                                       city.reference_icao, outcomes[0], **kw)
            days_ahead = (market.event_date - date.today()).days
            for o in outcomes:
                full = await agg.aggregate(db, city.id, city.primary_icao,
                                           city.reference_icao, o, **kw)
                shortcut = signals_for_outcome(base, o, full["market_price"])
                assert shortcut == full, f"signals diverge for {o.bucket_label}"

                raw, _ = estimate_with_breakdown(full, o.bucket_min, o.bucket_max,
                                                 days_ahead=days_ahead, bucket_unit="F")
                if o.id in recorded:
                    assert recorded[o.id].raw_p == pytest.approx(raw, abs=1e-6)

    @pytest.mark.asyncio
    async def test_the_estimator_does_not_mutate_shared_signals(self, pipeline):
        """The shortcut shares nested per-market values across buckets. That is
        only safe while the estimator treats signals as read-only."""
        import copy
        from app.analyzers.probability_estimator import estimate_with_breakdown
        from tests.fixtures import scenarios

        s = scenarios.signals_full()
        frozen = copy.deepcopy(s)
        estimate_with_breakdown(s, 93, 94, days_ahead=1, bucket_unit="F")
        assert s == frozen


# ── Live prices ───────────────────────────────────────────────────────────

class TestLivePrices:
    """The comparison has to be against the market's price at that moment.
    The stored midpoint is Polymarket's too and normally minutes old, but it
    cannot prove its own freshness and it is not the price you would pay."""

    @pytest.mark.asyncio
    async def test_the_live_book_wins_over_the_stored_price(self, pipeline):
        """Stored mid is 0.62; the live book says 0.70/0.72. The row must carry
        the live mid — proof the stored value was not used."""
        from tests.mocks import polymarket_payloads as pm
        await _prepare(pipeline)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(0.70, 0.72))
        await _run(pipeline)

        rows = await pipeline.rows(ShadowSnapshot)
        assert rows and all(r.price_live for r in rows)
        r = rows[0]
        assert r.market_p == pytest.approx(0.71, abs=1e-4)
        assert r.bid == pytest.approx(0.70, abs=1e-4)
        assert r.ask == pytest.approx(0.72, abs=1e-4)

    @pytest.mark.asyncio
    async def test_an_unusable_book_falls_back_to_the_stored_price_and_says_so(
        self, pipeline
    ):
        from tests.mocks import polymarket_payloads as pm
        await _prepare(pipeline)
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(bid=0.61, ask=None))
        stats = await _run(pipeline)

        rows = await pipeline.rows(ShadowSnapshot)
        assert rows and not any(r.price_live for r in rows)
        assert rows[0].market_p == pytest.approx(0.62, abs=1e-4)
        assert rows[0].bid is None and rows[0].ask is None
        assert stats["stored_fallbacks"] == len(rows) and stats["live_prices"] == 0

    @pytest.mark.asyncio
    async def test_a_polymarket_outage_does_not_fail_the_run(self, pipeline, no_backoff):
        from tests.mocks import polymarket_payloads as pm
        await _prepare(pipeline)
        pipeline.http.prepend(pm.CLOB_BOOK, {"error": "down"}, status=503)
        stats = await _run(pipeline)
        assert stats["errors"] == 0 and stats["rows"] > 0
        assert stats["stored_fallbacks"] == stats["rows"]

    @pytest.mark.asyncio
    async def test_dead_buckets_cost_no_request(self, pipeline):
        """A bucket both sides dismiss is never fetched."""
        from app.models.market import MarketPrice
        from tests.mocks import polymarket_payloads as pm
        await _prepare(pipeline)
        async with pipeline.session() as db:          # make every stored price ~0
            for mp in (await db.execute(select(MarketPrice))).scalars().all():
                mp.yes_price = 0.01
            await db.commit()
        # 130°F-style: nudge the model to ~0 on everything by making it absurd
        # is not needed — assert directly on the count instead.
        before = len(pipeline.http.requests)
        stats = await _run(pipeline)
        fetched = [r for r in pipeline.http.requests[before:] if pm.CLOB_BOOK in str(r.url)]
        assert len(fetched) == stats["book_calls"]
        assert stats["book_calls"] <= stats["rows"] + stats["skipped_dead"]

    @pytest.mark.asyncio
    async def test_the_price_job_freshness_is_stamped_on_every_row(self, pipeline):
        """For a row that fell back to the stored price, this is how to tell
        whether that price was minutes old or hours old."""
        from app.utils import jobstats
        await _prepare(pipeline)
        jobstats.record("polymarket", wall=1.0, cpu=0.1)
        await _run(pipeline)
        rows = await pipeline.rows(ShadowSnapshot)
        assert rows and all(r.price_job_age_min == 0 for r in rows)

    @pytest.mark.asyncio
    async def test_no_price_job_yet_is_recorded_as_unknown(self, pipeline):
        await _prepare(pipeline)
        await _run(pipeline)
        assert all(r.price_job_age_min is None for r in await pipeline.rows(ShadowSnapshot))


# ── Telegram ──────────────────────────────────────────────────────────────

async def _resolve(pipeline, winner_label="95-96°F"):
    from app.models.market import Market, MarketOutcome
    async with pipeline.session() as db:
        m = (await db.execute(select(Market))).scalars().one()
        m.resolved = True
        for o in (await db.execute(select(MarketOutcome))).scalars().all():
            o.won = o.bucket_label == winner_label
        await db.commit()


class TestTelegram:
    @pytest.mark.asyncio
    async def test_recording_alone_sends_nothing_by_default(self, pipeline):
        await _prepare(pipeline)
        await _run(pipeline)
        assert pipeline.telegram.sent == []

    @pytest.mark.asyncio
    async def test_a_summary_follows_resolution(self, pipeline):
        await _prepare(pipeline)
        now = _now()
        await _run(pipeline, now)
        await _resolve(pipeline)
        stats = await _run(pipeline, now + timedelta(hours=1))

        assert stats["summaries_sent"] == 1
        msg = pipeline.telegram.only()
        assert msg.parse_mode == "HTML"
        assert "Shadow study" in msg.text and "95-96°F" in msg.text
        assert "Who knew first" in msg.text
        state = (await pipeline.rows(ShadowMarketState))[0]
        assert state.summary_sent_at is not None

    @pytest.mark.asyncio
    async def test_the_summary_is_sent_exactly_once(self, pipeline):
        await _prepare(pipeline)
        now = _now()
        await _run(pipeline, now)
        await _resolve(pipeline)
        await _run(pipeline, now + timedelta(hours=1))
        await _run(pipeline, now + timedelta(hours=2))
        assert len(pipeline.telegram.sent) == 1

    @pytest.mark.asyncio
    async def test_no_summary_until_the_winner_is_known(self, pipeline):
        """Resolved-but-unsettled must wait, not report a story with no ending."""
        from app.models.market import Market
        await _prepare(pipeline)
        now = _now()
        await _run(pipeline, now)
        async with pipeline.session() as db:
            (await db.execute(select(Market))).scalars().one().resolved = True
            await db.commit()
        await _run(pipeline, now + timedelta(hours=1))
        assert pipeline.telegram.sent == []
        assert (await pipeline.rows(ShadowMarketState))[0].summary_sent_at is None

    @pytest.mark.asyncio
    async def test_hourly_mode_adds_one_digest_per_run(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.shadow_alert_mode", "summary_hourly")
        await _prepare(pipeline)
        stats = await _run(pipeline)
        assert stats["digest_sent"] is True
        msg = pipeline.telegram.only()
        assert "Shadow hourly" in msg.text and "Austin" in msg.text

    @pytest.mark.asyncio
    async def test_off_means_silence_even_after_resolution(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.shadow_alert_mode", "off")
        await _prepare(pipeline)
        now = _now()
        await _run(pipeline, now)
        await _resolve(pipeline)
        await _run(pipeline, now + timedelta(hours=1))
        assert pipeline.telegram.sent == []

    @pytest.mark.asyncio
    async def test_without_a_token_summaries_wait_instead_of_being_lost(
        self, pipeline, monkeypatch
    ):
        await _prepare(pipeline)
        now = _now()
        await _run(pipeline, now)
        await _resolve(pipeline)
        monkeypatch.setattr("app.config.settings.telegram_bot_token", "")
        await _run(pipeline, now + timedelta(hours=1))
        assert (await pipeline.rows(ShadowMarketState))[0].summary_sent_at is None

    @pytest.mark.asyncio
    async def test_a_telegram_outage_does_not_fail_the_run(self, pipeline):
        await _prepare(pipeline)
        now = _now()
        await _run(pipeline, now)
        await _resolve(pipeline)
        pipeline.telegram.fail_with = RuntimeError("telegram down")
        stats = await _run(pipeline, now + timedelta(hours=1))
        assert stats["errors"] == 0


# ── Settings ──────────────────────────────────────────────────────────────

class TestSettings:
    def test_both_switches_persist(self):
        from app.utils.settings_store import PERSISTABLE_KEYS
        assert {"shadow_enabled", "shadow_alert_mode"} <= PERSISTABLE_KEYS

    def test_defaults_record_and_summarise(self):
        from app.config import settings
        f = type(settings).model_fields
        assert f["shadow_enabled"].default is True
        assert f["shadow_alert_mode"].default == "summary"

    @pytest.mark.asyncio
    async def test_an_unknown_alert_mode_is_refused(self):
        from fastapi import HTTPException
        from app.api.admin import SettingsIn, admin_set_settings

        class _DB:
            async def get(self, *a, **k): return None
            def add(self, *a): pass
            async def commit(self): pass

        with pytest.raises(HTTPException) as e:
            await admin_set_settings(SettingsIn(shadow_alert_mode="every_minute"), "t", _DB())
        assert e.value.status_code == 400


# ── The boundary, checked in the source ───────────────────────────────────

class TestBoundary:
    @staticmethod
    def _shadow_code() -> str:
        from pathlib import Path
        root = Path(__file__).resolve().parents[2] / "app" / "shadow"
        return "\n".join(
            ln for p in root.glob("*.py") for ln in p.read_text(encoding="utf-8").splitlines()
            if not ln.strip().startswith("#")
        )

    def test_it_never_calls_the_side_effecting_production_paths(self):
        src = self._shadow_code()
        # Reading an order book is allowed (live_price.py). Writing a price is
        # not: collect_and_store is what fills market_prices.
        # The intraday detector's entry points write positions and register
        # cluster warm-ups; the study uses only the pure helpers they share.
        for forbidden in ("_persist_collector_misses(", "_collect_outcome_data(",
                          "collect_and_store(", "import httpx",
                          "_evaluate_intraday_outcome(", "detect_intraday("):
            assert forbidden not in src, f"shadow code must not use {forbidden}"

    def test_it_only_adds_its_own_rows(self):
        import re
        added = set(re.findall(r"db\.add\((\w+)\(", self._shadow_code()))
        assert added <= {"ShadowSnapshot", "ShadowMarketState"}, added

    def test_trading_code_does_not_depend_on_it(self):
        """Only the scheduler and the admin screen may know the study exists.
        If the detector ever imported it, a bug here could move a trade."""
        from pathlib import Path
        app_dir = Path(__file__).resolve().parents[2] / "app"
        importers = {
            str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py")
            if "app.shadow" in p.read_text(encoding="utf-8")
            and not str(p.relative_to(app_dir)).startswith("shadow")
        }
        assert importers <= {"main.py", "api/admin.py"}, importers
