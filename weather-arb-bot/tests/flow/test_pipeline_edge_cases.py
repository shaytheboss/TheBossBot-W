"""What the pipeline does when the world misbehaves.

Two questions every case here answers:

  1. Does the bot survive?  It must never crash a collection or detection
     cycle — one dead provider cannot stop the other ten.
  2. Does it stay honest?   Surviving by inventing a number is worse than
     failing. Degraded input must produce a degraded (or absent) signal, not
     a confident one.

Where a guard suppresses a trade, the test also shows the value *did* flow
through — by relaxing the guard and watching the signal appear. Otherwise
"no opportunity" is indistinguishable from "silently dropped the data".
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.forecast import Forecast
from app.models.market import MarketPrice
from app.models.opportunity import Opportunity
from tests.mocks import polymarket_payloads as pm
from tests.mocks import weather_payloads as wx


def _relax_thresholds(monkeypatch, alert: float = 0.30, edge: float = 0.99) -> None:
    """Lower the alert floor and the edge cap so a suppressed-but-real signal
    becomes visible. Used only to prove data reached the estimator."""
    monkeypatch.setattr("app.config.settings.min_confidence_alert_near", alert)
    monkeypatch.setattr("app.config.settings.min_confidence_alert_far", alert)
    monkeypatch.setattr("app.config.settings.max_edge_for_alert", edge)


# ── A single provider fails ───────────────────────────────────────────────

class TestOneSourceFails:
    @pytest.mark.asyncio
    async def test_a_dead_provider_does_not_stop_the_others(
        self, pipeline, break_source, no_backoff
    ):
        break_source(pipeline.http, wx.NWS_GRIDPOINT, status=503)
        stored = await pipeline.collect_forecasts()

        assert stored["nws"] is False
        assert all(stored[s] for s in ("gfs", "ecmwf", "hrrr", "icon"))
        assert await pipeline.count(Forecast) == 4

    @pytest.mark.asyncio
    async def test_the_cycle_still_trades_on_the_survivors(
        self, pipeline, break_source, no_backoff
    ):
        break_source(pipeline.http, wx.NWS_GRIDPOINT, status=503)
        result = await pipeline.run()

        assert result.bucket_sides() == {"93-94°F": "NO"}
        assert len(pipeline.telegram.sent) == 1

    @pytest.mark.asyncio
    async def test_losing_a_conus_only_source_does_not_count_as_missing_data(
        self, pipeline, break_source, no_backoff
    ):
        """NWS is CONUS-only and excluded from the global-source count, so its
        absence must not trigger the sparse-source shrink. Confidence is
        therefore unchanged — the property that lets London trade at all.

        The healthy baseline (83%) is asserted in `test_side_and_bucket_are
        _coherent`; it cannot be re-measured inside this test because the
        alert cooldown suppresses a second detection pass on the same market.
        """
        break_source(pipeline.http, wx.NWS_GRIDPOINT, status=503)
        degraded = await pipeline.run()

        assert degraded.best is not None, "four global models are still reporting"
        assert degraded.best.confidence_score == 83

    @pytest.mark.asyncio
    async def test_a_transient_5xx_is_retried_then_succeeds(
        self, pipeline, no_backoff
    ):
        import httpx
        pipeline.http.prepend_sequence(wx.NWS_GRIDPOINT, [
            httpx.Response(503, json={"error": "overloaded"}),
            httpx.Response(200, json=wx.nws_forecast(pipeline.event_date, 95, 74)),
        ], once=True)

        assert (await pipeline.collect_forecasts(["nws"]))["nws"] is True
        assert pipeline.http.count(wx.NWS_GRIDPOINT) == 2, "retried exactly once"
        assert await pipeline.count(Forecast) == 1, "the retry must not double-write"

    @pytest.mark.asyncio
    async def test_an_auth_error_is_not_retried(self, pipeline, no_backoff):
        """401 is deterministic. Retrying it three times with backoff wastes
        14 seconds per cycle and cannot succeed."""
        pipeline.http.prepend(wx.NWS_GRIDPOINT, {"error": "unauthorized"}, status=401)

        assert (await pipeline.collect_forecasts(["nws"]))["nws"] is False
        assert pipeline.http.count(wx.NWS_GRIDPOINT) == 1

    @pytest.mark.asyncio
    async def test_http_200_with_the_wrong_date_is_a_miss_not_a_crash(self, pipeline):
        """The real 'collector miss' shape: the request succeeded, so nothing
        raises, but the payload has no row for the day we asked about."""
        pipeline.http.prepend(
            wx.OPEN_METEO, wx.open_meteo_missing_date(pipeline.event_date)
        )
        stored = await pipeline.collect_forecasts()

        assert not any(stored[s] for s in ("gfs", "ecmwf", "hrrr", "icon"))
        assert stored["nws"] is True

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_contained(self, pipeline):
        pipeline.http.prepend(wx.OPEN_METEO, text="<html>502 Bad Gateway</html>")
        stored = await pipeline.collect_forecasts()

        assert stored["nws"] is True
        assert await pipeline.count(Forecast) == 1

    @pytest.mark.asyncio
    async def test_losing_every_global_model_stops_trading(
        self, pipeline, break_source, no_backoff
    ):
        """Open-Meteo carries GFS, ECMWF, ICON and HRRR. With it down only
        NWS survives — and NWS is CONUS-only, so zero global models remain.
        Refusing to bet is the correct outcome, not a bug."""
        break_source(pipeline.http, "open-meteo.com", status=503)
        result = await pipeline.run()

        assert await pipeline.count(Forecast) == 1
        assert result.opportunities == []
        assert pipeline.telegram.sent == []

    @pytest.mark.asyncio
    async def test_no_forecasts_at_all_is_silent_not_fatal(
        self, pipeline, break_source, no_backoff
    ):
        break_source(pipeline.http, "open-meteo.com", status=503)
        break_source(pipeline.http, "api.weather.gov", status=503)
        await pipeline.run()

        assert await pipeline.count(Forecast) == 0
        assert await pipeline.count(Opportunity) == 0
        assert pipeline.telegram.sent == []


# ── Extreme temperatures ──────────────────────────────────────────────────

class TestExtremeTemperatures:
    """130°F and -25°F are far outside every bucket on the ladder (91-98°F).

    The estimator gives each bucket a deterministic probability near zero —
    correct. Normalisation then spreads the mass across the four buckets, so
    each lands near 25% and P(NO) near 75%: below the 80% alert floor. The bot
    stays out, which is right, and the tests below show it reached that answer
    by reasoning about the data rather than by dropping it.
    """

    @pytest.mark.parametrize("forecast_temp", [130.0], indirect=True)
    @pytest.mark.asyncio
    async def test_absurd_heat_is_stored_faithfully(self, pipeline):
        await pipeline.collect_forecasts()
        assert {f.predicted_high_f for f in await pipeline.rows(Forecast)} == {130}

    @pytest.mark.parametrize("forecast_temp", [130.0], indirect=True)
    @pytest.mark.asyncio
    async def test_absurd_heat_produces_no_bet(self, pipeline):
        result = await pipeline.run()
        assert result.opportunities == []
        assert pipeline.telegram.sent == []

    @pytest.mark.parametrize("forecast_temp", [-25.0], indirect=True)
    @pytest.mark.asyncio
    async def test_absurd_cold_produces_no_bet(self, pipeline):
        result = await pipeline.run()
        assert result.opportunities == []
        assert pipeline.telegram.sent == []

    @pytest.mark.parametrize("forecast_temp", [130.0], indirect=True)
    @pytest.mark.asyncio
    async def test_the_extreme_value_did_reach_the_estimator(
        self, pipeline, monkeypatch
    ):
        """The load-bearing test of this class. Drop the alert floor and every
        bucket turns into a NO at ~75% — so the 130°F genuinely propagated and
        was judged, rather than being swallowed by an exception somewhere."""
        _relax_thresholds(monkeypatch)
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        result = await pipeline.detect()

        assert len(result.opportunities) == 4, "every bucket should be rejected"
        assert {o.side for o in result.opportunities} == {"NO"}
        assert all(70 <= o.confidence_score <= 80 for o in result.opportunities)

    @pytest.mark.parametrize("forecast_temp", [-25.0], indirect=True)
    @pytest.mark.asyncio
    async def test_heat_and_cold_are_treated_symmetrically(
        self, pipeline, monkeypatch
    ):
        """A sign error in the bucket maths would show up as one extreme
        behaving differently from the other."""
        _relax_thresholds(monkeypatch)
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        result = await pipeline.detect()

        assert len(result.opportunities) == 4
        assert {o.side for o in result.opportunities} == {"NO"}
        assert all(70 <= o.confidence_score <= 80 for o in result.opportunities)

    @pytest.mark.parametrize("forecast_temp", [130.0], indirect=True)
    @pytest.mark.asyncio
    async def test_no_bucket_probability_escapes_zero_to_one(
        self, pipeline, monkeypatch
    ):
        """Normalisation multiplies probabilities and can overshoot; the clip
        after it is what keeps an extreme input from yielding p > 1."""
        _relax_thresholds(monkeypatch)
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        await pipeline.detect()

        for opp in await pipeline.rows(Opportunity):
            assert 0.0 < float(opp.estimated_true_prob) < 1.0


# ── Models disagreeing ────────────────────────────────────────────────────

class TestModelDisagreement:
    @pytest.mark.parametrize("source_spread", [14.0], indirect=True)
    @pytest.mark.asyncio
    async def test_wide_disagreement_does_not_raise_confidence(self, pipeline):
        """Sources fanned 14°F apart. Whatever the bot concludes, it must not
        be *more* certain than when every model agreed (83%)."""
        result = await pipeline.run()
        if result.best is not None:
            assert result.best.confidence_score <= 83

    @pytest.mark.parametrize("source_spread", [14.0], indirect=True)
    @pytest.mark.asyncio
    async def test_disagreement_does_not_crash_the_cycle(self, pipeline):
        result = await pipeline.run()
        assert await pipeline.count(Forecast) == 6
        assert await pipeline.count(Opportunity) == len(result.opportunities)


# ── Market-side problems ──────────────────────────────────────────────────

class TestMarketProblems:
    @pytest.mark.asyncio
    async def test_without_a_price_no_outcome_is_evaluated(self, pipeline):
        """`_collect_outcome_data` returns None with no price. Estimating
        edge against a price we never fetched would be inventing one."""
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        result = await pipeline.detect()

        assert await pipeline.count(MarketPrice) == 0
        assert result.opportunities == []

    @pytest.mark.asyncio
    async def test_a_dead_price_feed_leaves_the_table_empty(self, pipeline):
        pipeline.http.prepend(pm.CLOB_MIDPOINT, {"error": "gone"}, status=404)
        assert await pipeline.collect_prices() == {"written": 0, "skipped": 0}
        assert await pipeline.count(MarketPrice) == 0

    @pytest.mark.asyncio
    async def test_a_one_sided_book_still_detects(self, pipeline):
        """No ask means nothing executable, so `get_book_summary` returns
        None. The outcome still takes part in normalisation — dropping it
        would redistribute its probability onto the others and inflate them."""
        pipeline.http.prepend(pm.CLOB_BOOK, pm.book(bid=0.61, ask=None))
        result = await pipeline.run()
        assert result.opportunities, "detection must survive an unquotable book"

    @pytest.mark.asyncio
    async def test_a_crossed_book_is_rejected(self, pipeline):
        """Bid above ask is impossible on a real venue, so the book is
        treated as absent rather than trusted — same handling as one-sided."""
        pipeline.http.prepend(pm.CLOB_BOOK, pm.crossed_book(bid=0.70, ask=0.60))

        from app.collectors.polymarket_collector import PolymarketCollector
        assert await PolymarketCollector().get_book_summary("tok0") is None

        result = await pipeline.run()
        assert result.opportunities, "detection continues without a usable book"

    @pytest.mark.asyncio
    async def test_a_resolved_market_is_skipped(self, pipeline):
        await pipeline.set_market(resolved=True)
        result = await pipeline.run()
        assert result.opportunities == [] and pipeline.telegram.sent == []

    @pytest.mark.parametrize("days_ahead", [3], indirect=True)
    @pytest.mark.asyncio
    async def test_the_last_day_inside_the_horizon_still_trades(self, pipeline):
        """The control for the test below. Without it, "day 4 is silent"
        could just mean the forecast got too uncertain to signal."""
        result = await pipeline.run()
        assert result.bucket_sides() == {"93-94°F": "NO"}

    @pytest.mark.parametrize("days_ahead", [4], indirect=True)
    @pytest.mark.asyncio
    async def test_one_day_past_the_horizon_is_skipped(self, pipeline):
        """`max_days_ahead_for_alert` is 3. Identical data one day further
        out must produce nothing — the guard, not the uncertainty, is what
        stops it."""
        result = await pipeline.run()
        assert result.opportunities == []
        assert pipeline.telegram.sent == []

    @pytest.mark.parametrize("days_ahead", [10], indirect=True)
    @pytest.mark.asyncio
    async def test_a_distant_market_is_skipped(self, pipeline):
        result = await pipeline.run()
        assert result.opportunities == []

    @pytest.mark.asyncio
    async def test_a_past_market_is_skipped(self, pipeline):
        await pipeline.set_market(event_date=date.today() - timedelta(days=1))
        result = await pipeline.run()
        assert result.opportunities == []


# ── City-level guards ─────────────────────────────────────────────────────

class TestCityGuards:
    @pytest.mark.asyncio
    async def test_a_blacklisted_city_alerts_but_never_bets(
        self, pipeline, monkeypatch
    ):
        """The documented contract: keep learning from the city, commit no
        simulated money. Buy threshold is lowered so the only thing stopping
        the position is the blacklist itself."""
        monkeypatch.setattr("app.config.settings.min_confidence_buy_near", 0.80)
        monkeypatch.setattr("app.config.settings.min_confidence_buy_far", 0.80)
        await pipeline.set_city(blacklisted=True)
        result = await pipeline.run()

        assert result.opportunities, "a blacklisted city must still alert"
        assert len(pipeline.telegram.sent) == 1
        opp = (await pipeline.rows(Opportunity))[0]
        assert opp.virtual_shares is None and opp.virtual_status is None

    @pytest.mark.asyncio
    async def test_an_identical_city_that_is_not_blacklisted_does_bet(
        self, pipeline, monkeypatch
    ):
        """The control for the test above — without it, `virtual_shares is
        None` could just mean the threshold was never cleared."""
        monkeypatch.setattr("app.config.settings.min_confidence_buy_near", 0.80)
        monkeypatch.setattr("app.config.settings.min_confidence_buy_far", 0.80)
        await pipeline.run()
        assert (await pipeline.rows(Opportunity))[0].virtual_status == "open"


# ── Delivery problems ─────────────────────────────────────────────────────

class TestDeliveryProblems:
    @pytest.mark.asyncio
    async def test_telegram_being_down_does_not_lose_the_opportunity(self, pipeline):
        """Delivery is the last step and the least important one: the row is
        the record. Losing it because a send failed would be a real bug."""
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        result = await pipeline.detect()

        pipeline.telegram.fail_with = RuntimeError("telegram unreachable")
        await pipeline.alert(result)

        assert pipeline.telegram.sent == []
        assert await pipeline.count(Opportunity) == 1

    @pytest.mark.asyncio
    async def test_no_subscribers_is_not_an_error(self, pipeline):
        await pipeline.clear_subscribers()
        result = await pipeline.run()

        assert result.opportunities
        assert pipeline.telegram.sent == []
        assert await pipeline.count(Opportunity) == 1

    @pytest.mark.asyncio
    async def test_a_subscriber_above_the_confidence_filter_gets_nothing(
        self, pipeline
    ):
        from sqlalchemy import select
        from app.models.alert import TelegramUser
        async with pipeline.session() as db:
            user = (await db.execute(select(TelegramUser))).scalars().one()
            user.min_confidence = 99
            await db.commit()

        result = await pipeline.run()
        assert result.opportunities and pipeline.telegram.sent == []


# ── Station bias ──────────────────────────────────────────────────────────

class TestBiasCorrection:
    """The learned airport-vs-official offset, applied before the bucket CDF.

    It has to be injected rather than accumulated: `bias_estimator` derives it
    from a raw Postgres query (`AT TIME ZONE`, `ANY(...)`, `INTERVAL`) that
    SQLite cannot parse. The function catches that itself and returns the
    +1.5°F default prior, so every other test in this file runs on the prior —
    these are the tests that exercise a real bias.
    """

    @staticmethod
    def _bias(monkeypatch, *, overall: float = 0.0, per_source: dict | None = None):
        async def learned(*args, **kwargs):
            return {
                "bias_f": overall,
                "per_source": per_source or {},
                "samples": 40,
                "notes": "injected by test",
                "is_default": False,
            }
        monkeypatch.setattr(
            "app.analyzers.signal_aggregator.get_station_bias", learned
        )

    @pytest.mark.asyncio
    async def test_the_default_prior_is_what_runs_without_history(self, pipeline):
        """A new station has no samples, so the +1.5°F prior applies. Every
        other flow test inherits this; asserting it here stops the prior from
        drifting unnoticed."""
        from app.analyzers.bias_estimator import DEFAULT_BIAS_F
        assert DEFAULT_BIAS_F == pytest.approx(1.5)

        result = await pipeline.run()
        assert result.bucket_sides() == {"93-94°F": "NO"}

    @pytest.mark.asyncio
    async def test_a_warm_bias_pushes_the_forecast_up(self, pipeline, monkeypatch):
        """+4°F on a 95°F consensus behaves like ~99°F, so the buckets below
        it become confident NOs that the unbiased run never produced."""
        self._bias(monkeypatch, overall=4.0)
        result = await pipeline.run()

        sides = result.bucket_sides()
        assert "91-92°F" in sides and "95-96°F" in sides
        assert set(sides.values()) == {"NO"}

    @pytest.mark.asyncio
    async def test_a_cool_bias_pulls_the_forecast_down(self, pipeline, monkeypatch):
        """−3°F lands the effective high near 92, straddling the 91-92 / 93-94
        boundary — so nothing is confident enough to bet. Opposite sign,
        opposite effect: the correction is not being applied as an absolute."""
        self._bias(monkeypatch, overall=-3.0)
        result = await pipeline.run()
        assert result.opportunities == []

    @pytest.mark.asyncio
    async def test_a_per_source_bias_beats_the_overall_one(self, pipeline, monkeypatch):
        """`per_source` wins over `bias_f` for the sources it names. With the
        overall bias at zero, any shift can only have come from GFS."""
        self._bias(monkeypatch, overall=0.0, per_source={"gfs": 6.0})
        result = await pipeline.run()

        assert "91-92°F" in result.bucket_sides()

    @pytest.mark.asyncio
    async def test_a_bias_for_an_absent_source_changes_nothing(
        self, pipeline, monkeypatch
    ):
        """Wunderground is not collected here, so a bias for it must be inert
        — proof the per-source map is keyed correctly and not applied blindly."""
        self._bias(monkeypatch, overall=1.5, per_source={"wunderground": 20.0})
        result = await pipeline.run()
        assert result.bucket_sides() == {"93-94°F": "NO"}


# ── Internal failure ──────────────────────────────────────────────────────

class TestInternalFailure:
    @pytest.mark.asyncio
    async def test_a_raising_bias_estimator_stops_trading_without_crashing(
        self, pipeline, monkeypatch
    ):
        """Documents current behaviour, which is safe but blunt.

        `signal_aggregator` calls `get_station_bias` unguarded. The function
        catches its own query errors and falls back to a default, so in
        production it does not raise — but if it ever did, every outcome in
        every market would be skipped by the detector's per-outcome handler
        and the bot would go quiet with only a log line to show for it.

        Failing closed is the right default. Worth knowing it fails *silently*.
        """
        async def boom(*args, **kwargs):
            raise RuntimeError("bias backend unavailable")

        monkeypatch.setattr(
            "app.analyzers.signal_aggregator.get_station_bias", boom
        )
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        result = await pipeline.detect()      # must not raise

        assert result.opportunities == []
        assert await pipeline.count(Opportunity) == 0

    @pytest.mark.asyncio
    async def test_a_raising_estimator_is_contained_per_outcome(
        self, pipeline, monkeypatch
    ):
        """One bad bucket must not take down the other three, or the market."""
        from app.analyzers import opportunity_detector as detector
        real = detector.estimate_with_breakdown
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("estimator blew up on bucket 1")
            return real(*args, **kwargs)

        monkeypatch.setattr(detector, "estimate_with_breakdown", flaky)
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        result = await pipeline.detect()      # must not raise

        assert calls["n"] == 4, "the remaining buckets must still be evaluated"
        assert isinstance(result.opportunities, list)
