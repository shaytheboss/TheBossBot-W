"""The whole bot, one stage at a time, on the happy path.

Mocked HTTP → real collectors → temporary database → real detector → real
Telegram formatting → FakeBot. Nothing between the edges is stubbed.

The world every test here starts in: Austin, a market resolving tomorrow
with four 2°F buckets from 91 to 98, every weather source reporting 95°F,
and a 62¢ two-sided Polymarket book.
"""
from __future__ import annotations

import pytest

from app.models.alert import Alert
from app.models.forecast import Forecast
from app.models.market import MarketPrice
from app.models.metar import MetarObservation
from app.models.opportunity import Opportunity
from tests.mocks import polymarket_payloads as pm
from tests.mocks import weather_payloads as wx


# ── Stage 1: collectors write forecasts ───────────────────────────────────

class TestForecastCollection:
    @pytest.mark.asyncio
    async def test_every_source_stores_one_row(self, pipeline):
        stored = await pipeline.collect_forecasts()
        assert stored == {"gfs": True, "ecmwf": True, "hrrr": True,
                          "icon": True, "nws": True}
        assert await pipeline.count(Forecast) == 5

    @pytest.mark.asyncio
    async def test_stored_values_are_the_mocked_ones(self, pipeline):
        await pipeline.collect_forecasts()
        rows = {f.source: f for f in await pipeline.rows(Forecast)}
        assert {s: r.predicted_high_f for s, r in rows.items()} == {
            "gfs": 95, "ecmwf": 95, "hrrr": 95, "icon": 95, "nws": 95
        }
        assert all(r.forecast_for_date == pipeline.event_date for r in rows.values())

    @pytest.mark.asyncio
    async def test_open_meteo_is_asked_for_the_right_model_each_time(self, pipeline):
        """Four collectors share one URL and differ only by `models`. If that
        parameter were wrong they would all silently store the same model."""
        await pipeline.collect_forecasts()
        asked = [r.url.params.get("models") for r in pipeline.http.calls_to(wx.OPEN_METEO)]
        assert {"gfs_seamless", "ecmwf_ifs025"} <= set(asked)
        assert any("hrrr" in a for a in asked) and any("icon" in a for a in asked)

    @pytest.mark.asyncio
    async def test_ensemble_is_stored_separately(self, pipeline):
        await pipeline.collect_forecasts()
        assert await pipeline.collect_ensemble() is True
        sources = {f.source for f in await pipeline.rows(Forecast)}
        assert "gfs_ensemble" in sources

    @pytest.mark.asyncio
    async def test_metar_upsert_runs_the_production_statement(self, pipeline):
        """`ON CONFLICT DO NOTHING` on the named constraint — not rewritten."""
        assert await pipeline.collect_metar() is True
        obs = await pipeline.rows(MetarObservation)
        assert len(obs) == 1 and obs[0].icao == "KAUS"

    @pytest.mark.asyncio
    async def test_metar_upsert_is_idempotent(self, pipeline):
        await pipeline.collect_metar()
        await pipeline.collect_metar()
        assert await pipeline.count(MetarObservation) == 1


# ── Stage 2: prices ───────────────────────────────────────────────────────

class TestPriceCollection:
    @pytest.mark.asyncio
    async def test_first_poll_writes_every_outcome(self, pipeline):
        assert await pipeline.collect_prices() == {"written": 4, "skipped": 0}
        assert await pipeline.count(MarketPrice) == 4

    @pytest.mark.asyncio
    async def test_unchanged_price_is_not_rewritten(self, pipeline):
        """The write-on-change rule that cut DB growth 68 → 13 MB/day, proven
        against a real table rather than a mocked session."""
        await pipeline.collect_prices()
        assert await pipeline.collect_prices() == {"written": 0, "skipped": 4}
        assert await pipeline.count(MarketPrice) == 4

    @pytest.mark.asyncio
    async def test_a_real_move_is_always_recorded(self, pipeline):
        await pipeline.collect_prices()
        pipeline.http.prepend(pm.CLOB_MIDPOINT, pm.midpoint(0.71))
        assert await pipeline.collect_prices() == {"written": 4, "skipped": 0}
        assert await pipeline.count(MarketPrice) == 8


# ── Stage 3: detection ────────────────────────────────────────────────────

class TestDetection:
    @pytest.mark.asyncio
    async def test_an_opportunity_is_found_and_persisted(self, pipeline):
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        result = await pipeline.detect()

        assert result.opportunities, "a 95°F consensus against 91-98 buckets must signal"
        assert await pipeline.count(Opportunity) == len(result.opportunities)

    @pytest.mark.asyncio
    async def test_it_bets_against_a_bucket_the_forecast_misses(self, pipeline):
        """Consensus is 95°F, so 93-94 cannot happen — NO is the right side."""
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        result = await pipeline.detect()
        assert result.bucket_sides() == {"93-94°F": "NO"}

    @pytest.mark.asyncio
    async def test_opportunity_fields_are_internally_consistent(self, pipeline):
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        await pipeline.detect()

        opp = (await pipeline.rows(Opportunity))[0]
        assert opp.estimator == "alpha"
        assert 0.0 < float(opp.estimated_true_prob) < 1.0
        assert 0.0 < float(opp.market_price) < 1.0
        assert opp.confidence_score == pytest.approx(
            round(float(opp.estimated_true_prob) * 100)
            if opp.side == "YES"
            else round((1 - float(opp.estimated_true_prob)) * 100),
            abs=1,
        )
        assert float(opp.edge) > 0, "an opportunity with no edge is not an opportunity"

    @pytest.mark.asyncio
    async def test_the_signals_that_produced_it_are_stored(self, pipeline):
        """`signals` is the audit trail: without it a past alert cannot be
        explained or re-derived."""
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        await pipeline.detect()

        signals = (await pipeline.rows(Opportunity))[0].signals
        assert signals["gfs_forecast"]["predicted_high_f"] == 95
        assert signals["nws_forecast"]["predicted_high_f"] == 95
        assert signals["market_price"] is not None
        assert signals["_bucket_min"] == 93 and signals["_bucket_max"] == 94

    @pytest.mark.asyncio
    async def test_a_virtual_position_opens_above_the_buy_threshold(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.min_confidence_buy_near", 0.80)
        monkeypatch.setattr("app.config.settings.min_confidence_buy_far", 0.80)
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        await pipeline.detect()

        opp = (await pipeline.rows(Opportunity))[0]
        assert opp.virtual_status == "open"
        assert opp.virtual_shares == 5
        assert 0.0 < opp.virtual_entry_price < 1.0
        assert opp.virtual_cost == pytest.approx(
            opp.virtual_shares * opp.virtual_entry_price, rel=1e-6
        )

    @pytest.mark.asyncio
    async def test_no_position_below_the_buy_threshold(self, pipeline):
        """Default buy gate is 90%; this signal is 83%, so it alerts only."""
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        await pipeline.detect()
        assert (await pipeline.rows(Opportunity))[0].virtual_shares is None

    @pytest.mark.asyncio
    async def test_the_second_pass_is_deduped(self, pipeline):
        """`alert_dedup_minutes` must stop the same market re-firing on the
        next 5-minute cycle."""
        await pipeline.collect_forecasts()
        await pipeline.collect_ensemble()
        await pipeline.collect_prices()
        first = await pipeline.detect()
        second = await pipeline.detect()

        assert first.opportunities and not second.opportunities
        assert await pipeline.count(Opportunity) == len(first.opportunities)


# ── Stage 4: Telegram ─────────────────────────────────────────────────────

class TestAlerting:
    @pytest.mark.asyncio
    async def test_the_alert_reaches_the_subscriber(self, pipeline):
        result = await pipeline.run()
        assert len(pipeline.telegram.sent) == len(result.opportunities)
        assert pipeline.telegram.only().chat_id == 999

    @pytest.mark.asyncio
    async def test_the_message_carries_the_decision(self, pipeline):
        """What the user actually reads has to name the city, the bucket, the
        side and the confidence — a formatter regression is a silent one."""
        await pipeline.run()
        text = pipeline.telegram.only().text
        assert "Austin" in text
        assert "93-94°F" in text
        assert "NO" in text
        assert "83%" in text
        assert "[α]" in text, "the estimator tag distinguishes alpha from beta"

    @pytest.mark.asyncio
    async def test_markdown_mode_is_the_legacy_dialect(self, pipeline):
        """MarkdownV2 would reject the unescaped `.`, `-`, `(` and `°` these
        messages contain, and Telegram would drop the alert."""
        await pipeline.run()
        assert pipeline.telegram.only().parse_mode == "Markdown"

    @pytest.mark.asyncio
    async def test_an_alert_row_records_the_delivery(self, pipeline):
        await pipeline.run()
        alerts = await pipeline.rows(Alert)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "OPPORTUNITY_DETECTED"
        assert alerts[0].telegram_message_id == 1
        assert alerts[0].city_id == pipeline.city.id

    @pytest.mark.asyncio
    async def test_the_opportunity_is_flagged_as_alerted(self, pipeline):
        await pipeline.run()
        assert (await pipeline.rows(Opportunity))[0].alert_sent is True


# ── The whole thing ───────────────────────────────────────────────────────

class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_one_full_cycle(self, pipeline):
        result = await pipeline.run()

        assert await pipeline.count(Forecast) == 6        # 5 models + ensemble
        assert await pipeline.count(MarketPrice) == 4     # one per bucket
        assert await pipeline.count(Opportunity) == 1
        assert await pipeline.count(Alert) == 1
        assert len(pipeline.telegram.sent) == 1
        assert result.bucket_sides() == {"93-94°F": "NO"}

    @pytest.mark.asyncio
    async def test_the_cycle_only_talked_to_expected_hosts(self, pipeline):
        """Proof the run was hermetic: every request went to a mocked host,
        and the socket guard would have raised for anything else."""
        await pipeline.run()
        hosts = {r.url.host for r in pipeline.http.requests}
        assert hosts <= {
            "api.open-meteo.com", "ensemble-api.open-meteo.com",
            "api.weather.gov", "aviationweather.gov", "clob.polymarket.com",
        }, f"unexpected host contacted: {hosts}"
        assert pipeline.http.count() > 0
