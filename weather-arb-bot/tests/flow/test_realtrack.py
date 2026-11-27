"""The real track end to end: mocked HTTP -> real collectors -> the real alpha
(and beta) detector -> hook.note -> track.process -> realtrack_buys ->
Telegram (FakeBot) -> the report.

The harness's signals top out around 83% (the same reason
test_full_pipeline lowers the buy gate to 0.80), so tests about the
mechanics set the floor to 0.83 and one test proves 0.84 refuses the same
signal. The 91% default itself is pinned in TestTheRule.

The city's local day is pinned through `hook._now`; the detector keeps its
own clock, exactly as in production.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select, update

import app.realtrack.hook as hook
import app.realtrack.track as track
from app.models.alert import Alert
from app.models.market import MarketOutcome
from app.models.opportunity import Opportunity
from app.realtrack.models import RealTrackBuy

FLOOR = 0.83          # the harness signal: 93-94°F NO at 83%
SLUG_LINK = "https://polymarket.com/event/highest-temperature-in-austin"


def _pin(monkeypatch, pipeline, *, local_day: date | None = None, hour: int = 12) -> datetime:
    """Make the city's own today `local_day` (default: the event date)."""
    tz = ZoneInfo(pipeline.city.tz)
    day = local_day or pipeline.event_date
    at = datetime.combine(day, time(hour, 0), tzinfo=tz).astimezone(timezone.utc)
    monkeypatch.setattr(hook, "_now", lambda: at)
    return at


async def _collect(pipeline):
    await pipeline.collect_forecasts()
    await pipeline.collect_ensemble()
    await pipeline.collect_prices()


async def _cycle(pipeline, now):
    """One analyzer cycle as job_run_analyzer runs it: detect, research
    alerts, then the real track."""
    result = await pipeline.detect()
    await pipeline.alert(result)
    async with pipeline.session() as db:
        stats = await track.process(db, now=now)
    return result, stats


def _real(pipeline):
    return [m for m in pipeline.telegram.sent if "REAL TRACK" in m.text]


def _research(pipeline):
    return [m for m in pipeline.telegram.sent if "REAL TRACK" not in m.text]


# ── The rule ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days_ahead", [0], indirect=True)
class TestTheRule:
    @pytest.mark.asyncio
    async def test_a_same_day_signal_at_the_floor_is_bought_at_the_live_price(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        _, stats = await _cycle(pipeline, now)

        assert stats == {"noted": 1, "new": 1, "alerted": 1}
        (b,) = await pipeline.rows(RealTrackBuy)
        assert (b.outcome_id, b.side, b.confidence, b.estimator) == (2, "NO", 83, "alpha")
        # The book is 0.61 / 0.63: a NO share costs 1 - the YES bid.
        assert b.bid == pytest.approx(0.61) and b.ask == pytest.approx(0.63)
        assert b.price == pytest.approx(0.39) and b.shares == 5
        assert b.event_date == pipeline.event_date and b.local_hour == 12.0
        assert b.research_open is False and b.alerted is True

    @pytest.mark.asyncio
    async def test_the_message_has_its_own_header_price_and_link(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        now = _pin(monkeypatch, pipeline, hour=13)
        await _collect(pipeline)
        await _cycle(pipeline, now)

        (m,) = _real(pipeline)
        assert m.parse_mode == "HTML" and m.chat_id == 999
        first = m.text.splitlines()[0]
        assert "REAL TRACK — BUY NO 93-94°F" in first and first.startswith("💰💰")
        assert "Austin · event today · 13:00 local" in m.text
        # Book 0.61 / 0.63 for YES is 0.37 / 0.39 for NO: prices in the side bought.
        assert "NO price <b>39.0¢</b> per share · NO bid 37.0¢ · spread 2.0¢" in m.text
        assert "YES bid" not in m.text and "63.0¢" not in m.text
        assert "Bot confidence <b>83%</b> [α]" in m.text and "≥83%" in m.text
        assert f'<a href="{SLUG_LINK}">Open on Polymarket</a>' in m.text
        assert "Virtual buy of 5 shares" in m.text
        assert "already holds" not in m.text and "so far" not in m.text
        # The research alert still goes out, unchanged, in its own dialect.
        (r,) = _research(pipeline)
        assert r.parse_mode == "Markdown" and "REAL TRACK" not in r.text

    @pytest.mark.asyncio
    async def test_a_signal_below_the_floor_is_not_bought(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR + 0.01)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        result, stats = await _cycle(pipeline, now)

        assert result.opportunities, "the research track still fires"
        assert stats["noted"] == 0 and await pipeline.count(RealTrackBuy) == 0
        assert not _real(pipeline) and len(_research(pipeline)) == 1

    @pytest.mark.asyncio
    async def test_the_default_floor_is_91_percent(self, pipeline, monkeypatch):
        from app.config import settings
        assert settings.realtrack_min_confidence == 0.91 and hook.min_confidence() == 91
        assert settings.realtrack_max_days_ahead == 0
        assert settings.realtrack_enabled is True and settings.realtrack_alerts_enabled is True
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        _, stats = await _cycle(pipeline, now)
        assert stats["noted"] == 0 and await pipeline.count(RealTrackBuy) == 0

    @pytest.mark.asyncio
    async def test_a_blacklisted_city_is_not_bought(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        await pipeline.set_city(blacklisted=True)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        await _cycle(pipeline, now)
        assert await pipeline.count(RealTrackBuy) == 0

    @pytest.mark.asyncio
    async def test_switched_off_nothing_is_noted(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        monkeypatch.setattr("app.config.settings.realtrack_enabled", False)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        _, stats = await _cycle(pipeline, now)
        assert stats["noted"] == 0 and await pipeline.count(RealTrackBuy) == 0
        assert not _real(pipeline)

    @pytest.mark.asyncio
    async def test_alerts_off_still_records_the_buy(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        monkeypatch.setattr("app.config.settings.realtrack_alerts_enabled", False)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        _, stats = await _cycle(pipeline, now)
        (b,) = await pipeline.rows(RealTrackBuy)
        assert b.alerted is False and stats["alerted"] == 0 and not _real(pipeline)

    @pytest.mark.asyncio
    async def test_a_stale_note_is_not_priced(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        await pipeline.detect()
        async with pipeline.session() as db:
            stats = await track.process(db, now=now + track.STALE + timedelta(minutes=1))
        assert stats["new"] == 0 and await pipeline.count(RealTrackBuy) == 0


@pytest.mark.parametrize("days_ahead", [1], indirect=True)
class TestTomorrow:
    @pytest.mark.asyncio
    async def test_a_market_for_tomorrow_in_the_city_is_not_bought(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        now = _pin(monkeypatch, pipeline, local_day=pipeline.event_date - timedelta(days=1))
        await _collect(pipeline)
        result, stats = await _cycle(pipeline, now)
        assert result.opportunities and stats["noted"] == 0
        assert await pipeline.count(RealTrackBuy) == 0

    @pytest.mark.asyncio
    async def test_widening_the_window_to_one_day_takes_it(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        monkeypatch.setattr("app.config.settings.realtrack_max_days_ahead", 1)
        now = _pin(monkeypatch, pipeline, local_day=pipeline.event_date - timedelta(days=1))
        await _collect(pipeline)
        await _cycle(pipeline, now)
        (b,) = await pipeline.rows(RealTrackBuy)
        assert b.side == "NO"
        assert "event in 1 day ·" in _real(pipeline)[0].text


class TestTheCitysCalendar:
    def test_the_lead_is_counted_in_the_city_not_on_the_server(self):
        # 03:00 UTC on the 21st is 21:00 on the 20th in Austin.
        at = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)
        assert hook.local_lead(date(2026, 11, 20), "America/Chicago", at) == 0
        assert hook.local_lead(date(2026, 11, 21), "America/Chicago", at) == 1
        # 20:00 UTC on the 21st is already the 22nd in Tokyo.
        at = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
        assert hook.local_lead(date(2026, 11, 22), "Asia/Tokyo", at) == 0
        assert hook.local_lead(date(2026, 11, 21), "Asia/Tokyo", at) == -1

    def test_the_floor_compares_the_rounded_percent(self, monkeypatch):
        """The rule was measured on the stored confidence_score, which is
        int(round(certainty * 100)); the hook rounds the same way, so a signal
        is taken exactly when its stored confidence would read 91 or more."""
        assert hook.qualifies(91, 0) and not hook.qualifies(90, 0)
        assert not hook.qualifies(95, 1) and not hook.qualifies(95, -1)

        class C:
            id, name, timezone = 7, "Tokyo", "Asia/Tokyo"

        class M:
            id, external_id, event_date = 1, "s", date(2026, 11, 22)

        class O:
            id, bucket_label = 3, "15°C"
        monkeypatch.setattr(hook, "_now", lambda: datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc))
        kw = dict(estimator="alpha", city=C, market=M, outcome=O, side="NO",
                  book={"bid": 0.2, "ask": 0.22}, entry_cost=0.8)
        assert hook.note(certainty=0.906, **kw) is True          # stored as 91
        assert hook.note(certainty=0.9049, **kw) is False        # stored as 90
        assert hook.note(certainty=0.95, excluded=True, **kw) is False
        (n,) = hook.drain()
        assert n["confidence"] == 91 and n["lead"] == 0 and n["local_hour"] == 12.0
        assert hook.PENDING == []


# ── Next to the research track ────────────────────────────────────────────

@pytest.mark.parametrize("days_ahead", [0], indirect=True)
class TestAlongsideResearch:
    @pytest.mark.asyncio
    async def test_an_open_research_position_does_not_hide_the_same_day_entry(self, pipeline, monkeypatch):
        """The research dedup stops a second research record on a bucket that
        already has an open position. The real track is noted before that
        check, so it still takes the same-day entry — and says so."""
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        async with pipeline.session() as db:
            db.add(Opportunity(
                outcome_id=2, side="NO", detected_at=datetime.now(timezone.utc) - timedelta(days=2),
                market_price=0.5, estimated_true_prob=0.1, edge=0.2, confidence_score=92,
                signals={}, alert_sent=True, virtual_shares=5, virtual_entry_price=0.7,
                virtual_cost=3.5, virtual_status="open",
            ))
            await db.commit()
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        result, stats = await _cycle(pipeline, now)

        assert not result.by_bucket("93-94°F"), "research recorded nothing new on the held bucket"
        assert await pipeline.count(Opportunity) == 1, "and the real track wrote no research row"
        (b,) = await pipeline.rows(RealTrackBuy)
        assert b.outcome_id == 2 and b.research_open is True
        (m,) = _real(pipeline)
        assert "📌 The regular bot already holds NO on this bucket since" in m.text
        assert "at 70.0¢ — it records nothing new today; this is today's entry." in m.text

    @pytest.mark.asyncio
    async def test_only_a_position_held_from_before_counts_as_already_held(self, pipeline, monkeypatch):
        """At 91%+ the regular bot usually buys the same bucket in the same run
        (its own gate is 90%), seconds AFTER the real track noted it. That is
        not "already held" — only a position opened before the signal is."""
        t = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)

        def note(outcome_id):
            return {"estimator": "alpha", "city_id": pipeline.city.id, "city": "Austin",
                    "tz": pipeline.city.tz, "market_id": 1, "slug": "s", "event_date": pipeline.event_date,
                    "lead": 0, "outcome_id": outcome_id, "bucket": "x", "side": "NO", "confidence": 93,
                    "certainty": 0.93, "bid": 0.2, "ask": 0.22, "price": 0.8, "noted_at": t, "local_hour": 12.0}

        def research(outcome_id, at, price):
            return Opportunity(outcome_id=outcome_id, side="NO", detected_at=at, market_price=0.2,
                               estimated_true_prob=0.07, edge=0.15, confidence_score=93, signals={},
                               alert_sent=True, virtual_shares=5, virtual_entry_price=price,
                               virtual_cost=5 * price, virtual_status="open")
        async with pipeline.session() as db:
            db.add(research(1, t + timedelta(seconds=3), 0.8))      # bought in this same run
            db.add(research(2, t - timedelta(days=2), 0.71))         # bought two days earlier
            await db.commit()
        hook.PENDING.extend([note(1), note(2)])
        async with pipeline.session() as db:
            await track.process(db, now=t + timedelta(minutes=1))

        held = {b.outcome_id: b.research_open for b in await pipeline.rows(RealTrackBuy)}
        assert held == {1: False, 2: True}
        texts = sorted(m.text for m in _real(pipeline))
        assert sum("already holds" in x for x in texts) == 1
        assert any("already holds NO on this bucket since 19.11 at 71.0¢" in x for x in texts)

    @pytest.mark.asyncio
    async def test_the_research_track_is_identical_with_the_real_track_on_or_off(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)

        async def research_fingerprint():
            async with pipeline.session() as db:
                opps = (await db.execute(select(Opportunity))).scalars().all()
                rows = sorted((o.outcome_id, o.side, o.confidence_score, round(float(o.edge), 6),
                               o.virtual_status, o.virtual_shares, o.alert_sent) for o in opps)
            return rows, [(m.text, m.parse_mode) for m in _research(pipeline)]

        await _cycle(pipeline, now)
        on = await research_fingerprint()
        assert await pipeline.count(RealTrackBuy) == 1

        # Same world again with the real track off.
        async with pipeline.session() as db:
            for model in (Opportunity, Alert, RealTrackBuy):
                await db.execute(delete(model))
            await db.commit()
        pipeline.telegram.reset()
        monkeypatch.setattr("app.config.settings.realtrack_enabled", False)
        await _cycle(pipeline, now)
        off = await research_fingerprint()

        assert on == off and on[0], "the research rows and alerts do not depend on the real track"
        assert await pipeline.count(RealTrackBuy) == 0

    @pytest.mark.asyncio
    async def test_one_buy_per_bucket_and_side(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        monkeypatch.setattr("app.config.settings.alert_dedup_minutes", 0)   # re-evaluate every run
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        _, first = await _cycle(pipeline, now)
        _, second = await _cycle(pipeline, now + timedelta(minutes=5))

        assert first["new"] == 1 and second["noted"] == 1 and second["new"] == 0
        assert await pipeline.count(RealTrackBuy) == 1 and len(_real(pipeline)) == 1

    @pytest.mark.asyncio
    async def test_a_broken_hook_cannot_change_the_detector(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        _pin(monkeypatch, pipeline)

        def boom(*a, **k):
            raise RuntimeError("hook failure")
        monkeypatch.setattr(hook, "local_lead", boom)
        await _collect(pipeline)
        result = await pipeline.detect()

        assert result.bucket_sides() == {"93-94°F": "NO"}
        assert result.best.confidence_score == 83
        assert hook.PENDING == []

    @pytest.mark.asyncio
    async def test_the_beta_detector_notes_too(self, pipeline, monkeypatch):
        """Beta's blended certainty sits far below alpha's in the harness, so
        its thresholds are lowered here only to make it fire."""
        from app.analyzers.beta_opportunity_detector import detect_beta_opportunities
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", 0.60)
        monkeypatch.setattr("app.config.settings.min_confidence_alert_near", 0.5)
        monkeypatch.setattr("app.config.settings.min_edge_for_alert", 0.0)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        async with pipeline.session() as db:
            await detect_beta_opportunities(db)
        async with pipeline.session() as db:
            stats = await track.process(db, now=now)
        rows = await pipeline.rows(RealTrackBuy)
        assert stats["new"] == len(rows) >= 1
        assert {r.estimator for r in rows} == {"beta"} and all(r.confidence >= 60 for r in rows)
        assert all("[β]" in m.text for m in _real(pipeline))


# ── The report ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days_ahead", [0], indirect=True)
class TestReport:
    @pytest.mark.asyncio
    async def test_a_settled_buy_is_scored_at_its_price(self, pipeline, monkeypatch):
        from app.api.admin import admin_realtrack_report
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        now = _pin(monkeypatch, pipeline, hour=13)
        await _collect(pipeline)
        await _cycle(pipeline, now)

        async with pipeline.session() as db:
            rep = await admin_realtrack_report("t", db, days=30, from_date=None, to_date=None, city_id=None)
        assert rep["buys"] == 1 and rep["settled"] == 0 and rep["pnl_per_share"] is None
        (r,) = rep["rows"]
        assert r["result"] == "open" and r["link"] == SLUG_LINK and r["local_time"] == "13:00"
        assert (r["side"], r["bucket"], r["model"], r["confidence"]) == ("NO", "93-94°F", "alpha", 83)
        # Shown in the side bought: the YES book 0.61 / 0.63 is 0.37 / 0.39 for NO.
        assert (r["price"], r["bid"], r["ask"]) == (pytest.approx(0.39), pytest.approx(0.37), pytest.approx(0.39))
        assert r["spread"] == pytest.approx(0.02)

        # 95-96°F wins: the NO on 93-94°F pays 1.
        async with pipeline.session() as db:
            await db.execute(update(MarketOutcome).values(won=False))
            await db.execute(update(MarketOutcome).where(MarketOutcome.id == 3).values(won=True))
            await db.commit()
            rep = await admin_realtrack_report("t", db, days=30, from_date=None, to_date=None, city_id=None)
            other = await admin_realtrack_report("t", db, days=30, from_date=None, to_date=None,
                                                 city_id=pipeline.city.id + 1)
            later = await admin_realtrack_report("t", db, days=30, city_id=None, to_date=None,
                                                 from_date=pipeline.event_date + timedelta(days=1))
        assert rep["settled"] == rep["wins"] == 1
        assert rep["pnl_per_share"] == pytest.approx(0.61) and rep["total_pnl"] == pytest.approx(3.05)
        (c,) = rep["by_city"]
        assert (c["city"], c["buys"], c["settled"], c["wins"], c["enough"]) == ("Austin", 1, 1, 1, False)
        assert rep["rule"] == {"min_confidence": FLOOR, "max_days_ahead": 0, "enabled": True}
        assert other["buys"] == 0 and later["buys"] == 0

    @pytest.mark.asyncio
    async def test_the_next_message_carries_the_record(self, pipeline, monkeypatch):
        monkeypatch.setattr("app.config.settings.realtrack_min_confidence", FLOOR)
        now = _pin(monkeypatch, pipeline)
        await _collect(pipeline)
        await _cycle(pipeline, now)
        async with pipeline.session() as db:
            await db.execute(update(MarketOutcome).values(won=False))
            await db.commit()
            record = await track.track_record(db)
        assert record == {"settled": 1, "wins": 1, "pnl_per_share": pytest.approx(0.61)}
        text = track.message(
            {"side": "NO", "bucket": "15°C", "city": "Tokyo", "lead": 0, "local_hour": 9.5,
             "price": 0.8, "bid": 0.19, "ask": 0.21, "confidence": 93, "estimator": "beta",
             "slug": None}, None, record)
        assert "Real track so far: 1/1 won · +61.0¢ per share" in text
        assert "09:30 local" in text and "[β]" in text and "Open on Polymarket" not in text
        assert "NO price <b>80.0¢</b> per share · NO bid 79.0¢ · spread 2.0¢" in text
        yes = track.message(
            {"side": "YES", "bucket": "15°C", "city": "Tokyo", "lead": 0, "local_hour": 9.5,
             "price": 0.74, "bid": 0.71, "ask": 0.74, "confidence": 92, "estimator": "alpha",
             "slug": None}, None, {})
        assert "YES price <b>74.0¢</b> per share · YES bid 71.0¢ · spread 3.0¢" in yes


def test_the_book_of_the_side_bought():
    assert track.side_book("YES", 0.61, 0.63) == (0.61, 0.63)
    assert track.side_book("NO", 0.61, 0.63) == (pytest.approx(0.37), pytest.approx(0.39))
    assert track.side_book("NO", 0.22, 0.24) == (pytest.approx(0.76), pytest.approx(0.78))   # Jeddah 35°C


def test_the_tab_filters_and_its_cards_follow_them():
    """Like the Positions tab: side, minimum confidence, max price, local hour
    and "regular bot already held" — and the cards and the by-city table are
    recomputed from the rows the filters let through. (Behaviour checked in a
    headless browser; this keeps the wiring from silently coming undone.)"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[2] / "app" / "static" / "admin.html").read_text(encoding="utf-8")
    ids = ("rtSide", "rtHeld", "rtMinConf", "rtMinPrice", "rtMaxPrice", "rtHourFrom", "rtHourTo")
    apply_fn = html[html.index("function applyRtClientFilter()"):]
    apply_fn = apply_fn[:apply_fn.index("\n}\n")]
    reset_fn = html[html.index("function resetRtFilter()"):]
    reset_fn = reset_fn[:reset_fn.index("\n}\n")]
    for i in ids:
        assert f'id="{i}"' in html and f"'{i}'" in apply_fn and f"'{i}'" in reset_fn, i
    assert "_renderRtCards(rows, all.length)" in apply_fn and "_renderRtByCity(_rtByCity(rows))" in apply_fn
    assert "applyRtClientFilter();" in html[html.index("async function loadRealTrack()"):html.index("function _renderRtCards")]


def test_result_of_each_side():
    assert track.result_of("YES", True) is True and track.result_of("YES", False) is False
    assert track.result_of("NO", True) is False and track.result_of("NO", False) is True
    assert track.result_of("NO", None) is None


# ── Settings ──────────────────────────────────────────────────────────────

class TestSettings:
    @pytest.mark.asyncio
    async def test_the_switches_persist_and_are_validated(self, monkeypatch):
        from fastapi import HTTPException
        from app.api.admin import SettingsIn, admin_get_settings, admin_set_settings
        from app.config import settings
        from app.utils.settings_store import PERSISTABLE_KEYS
        from tests.flow.test_open_meteo_batch import _DB
        keys = ("realtrack_enabled", "realtrack_alerts_enabled",
                "realtrack_min_confidence", "realtrack_max_days_ahead")
        assert set(keys) <= PERSISTABLE_KEYS
        for k, v in zip(keys, (True, True, 0.91, 0)):
            monkeypatch.setattr(settings, k, v)

        got = await admin_get_settings("t")
        assert {k: got[k] for k in keys} == dict(zip(keys, (True, True, 0.91, 0)))

        await admin_set_settings(SettingsIn(realtrack_alerts_enabled=False, realtrack_min_confidence=0.93,
                                            realtrack_max_days_ahead=1), "t", _DB())
        assert settings.realtrack_alerts_enabled is False and settings.realtrack_enabled is True
        assert settings.realtrack_min_confidence == 0.93 and settings.realtrack_max_days_ahead == 1
        for bad in (SettingsIn(realtrack_min_confidence=0.4), SettingsIn(realtrack_max_days_ahead=4)):
            with pytest.raises(HTTPException):
                await admin_set_settings(bad, "t", _DB())


# ── Boundary ──────────────────────────────────────────────────────────────

class TestBoundary:
    def _sources(self):
        from pathlib import Path
        app_dir = Path(__file__).resolve().parents[2] / "app"
        return app_dir, {str(p.relative_to(app_dir)): p.read_text(encoding="utf-8")
                         for p in app_dir.rglob("*.py")}

    def test_who_may_know_the_real_track(self):
        """The detectors reach it only through the in-memory hook; the job
        runs it; the admin screen reports it. Nothing else."""
        _, src = self._sources()
        importers = {p for p, s in src.items() if "app.realtrack" in s and not p.startswith("realtrack")}
        assert importers == {"analyzers/opportunity_detector.py", "analyzers/beta_opportunity_detector.py",
                             "workers/jobs.py", "api/admin.py"}, importers
        for det in ("analyzers/opportunity_detector.py", "analyzers/beta_opportunity_detector.py"):
            lines = [ln for ln in src[det].splitlines() if "app.realtrack" in ln]
            assert lines == ["from app.realtrack import hook as realtrack_hook"], lines

    def test_the_hook_touches_no_database_or_network(self):
        _, src = self._sources()
        hook_src = src["realtrack/hook.py"]
        for word in ("sqlalchemy", "httpx", "telegram", "await "):
            assert word not in hook_src, word

    def test_the_real_track_never_writes_research_positions(self):
        _, src = self._sources()
        import re
        for path, s in src.items():
            if path.startswith("realtrack"):
                assert not re.search(r"\bOpportunity\(", s), path            # no new research row
                assert not re.search(r"virtual_\w+\s*=(?!=)", s), path       # no position field set
                assert not re.search(r"(update|delete|insert)\(Opportunity", s), path

    def test_it_reads_no_research_package(self):
        """Keeps the other boundary tests (shadow, nearby, peaks, research) true."""
        _, src = self._sources()
        for path, s in src.items():
            if path.startswith("realtrack"):
                for pkg in ("app.shadow", "app.nearby", "app.peaks", "app.research"):
                    assert pkg not in s, (path, pkg)
