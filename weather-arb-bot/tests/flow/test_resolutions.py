"""Settlement end to end: unresolved markets in a temporary database ->
job_check_resolutions -> mocked Gamma -> winners on MarketOutcome.won ->
research positions settled -> the Real track report.

The job runs hourly and checks a market once its day has ended in the city's
own calendar. Before, it ran daily and waited for UTC midnight, so a Seoul
market settled by Polymarket at 15:21 UTC stayed "open" for up to a day and a
half on every screen.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

import app.workers.jobs as jobs
from app.models.market import Market, MarketOutcome
from app.models.opportunity import Opportunity
from app.realtrack.models import RealTrackBuy
from tests.fixtures import cities as city_fixtures
from tests.mocks import polymarket_payloads as pm
from tests.mocks import sqlite_compat

DAY = date(2026, 11, 22)
SEOUL_SLUG = "highest-temperature-in-seoul-on-november-22-2026"
LA_SLUG = "highest-temperature-in-los-angeles-on-november-22-2026"
SEOUL_BUCKETS = {11: ("4°C", "s4"), 12: ("5°C", "s5"), 13: ("6°C", "s6")}
LA_BUCKETS = {21: ("64-65°F", "l64"), 22: ("66-67°F", "l66")}
BUCKETS = {SEOUL_SLUG: SEOUL_BUCKETS, LA_SLUG: LA_BUCKETS, "seoul-23": {31: ("7°C", "s7")}}


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc)


class Gamma:
    """Gamma /events by slug: settled events answer `closed=true` with a 1/0
    split; open ones only answer the plain query, with live prices."""

    def __init__(self):
        self.winners: dict[str, str] = {}          # slug -> winning YES token

    def __call__(self, request: httpx.Request) -> httpx.Response:
        slug = request.url.params.get("slug")
        buckets = BUCKETS[slug]
        winner = self.winners.get(slug)
        settled_query = request.url.params.get("closed") == "true" or request.url.params.get("active") == "false"
        if settled_query and winner is None:
            return httpx.Response(200, json=pm.no_events())
        markets = [pm.sub_market(question=f"{label}?", slug=f"{slug}-{tok}", token_ids=(tok, tok + "n"),
                                 won=None if winner is None else tok == winner)
                   for label, tok in buckets.values()]
        return httpx.Response(200, json=pm.events_response(
            pm.event(slug=slug, closed=winner is not None, markets=markets)))


@pytest.fixture
async def world(http, monkeypatch):
    from app.database import Base
    import app.models  # noqa: F401
    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    monkeypatch.setattr(jobs, "AsyncSessionLocal", maker)
    london = city_fixtures.get("London")
    seoul = london.as_row(id=50, name="Seoul", polymarket_slug="seoul", primary_icao="RKSI", timezone="Asia/Seoul")
    la = city_fixtures.get("Los Angeles").as_row()
    async with maker() as db:
        db.add_all([seoul, la])
        db.add(Market(id=1, city_id=50, external_id=SEOUL_SLUG, question="Seoul?", event_date=DAY, resolved=False))
        db.add(Market(id=2, city_id=la.id, external_id=LA_SLUG, question="LA?", event_date=DAY, resolved=False))
        for mid, buckets in ((1, SEOUL_BUCKETS), (2, LA_BUCKETS)):
            for oid, (label, tok) in buckets.items():
                unit = "C" if label.endswith("C") else "F"
                lo = int(label.split("-")[0].rstrip("°CF"))
                db.add(MarketOutcome(id=oid, market_id=mid, bucket_label=label, bucket_min=lo,
                                     bucket_max=lo if unit == "C" else lo + 1, bucket_unit=unit, token_id=tok))
        # The regular bot holds NO on 4°C; the real track bought NO on 5°C.
        db.add(Opportunity(outcome_id=11, side="NO", detected_at=_at(22, 3), market_price=0.2,
                           estimated_true_prob=0.07, edge=0.15, confidence_score=93, signals={},
                           alert_sent=True, virtual_shares=5, virtual_entry_price=0.8,
                           virtual_cost=4.0, virtual_status="open"))
        db.add(RealTrackBuy(estimator="alpha", city_id=50, market_id=1, outcome_id=12, side="NO",
                            event_date=DAY, bought_at=_at(22, 3), local_hour=12.0, confidence=92,
                            bid=0.2, ask=0.22, price=0.8, shares=5, research_open=False, alerted=True))
        await db.commit()
    gamma = Gamma()
    http.add_handler(pm.GAMMA_EVENTS, gamma)
    yield maker, http, gamma
    await engine.dispose()


async def _markets(maker) -> dict[int, bool]:
    async with maker() as db:
        return {m.id: bool(m.resolved) for m in (await db.execute(select(Market))).scalars().all()}


class TestTheCitysDay:
    @pytest.mark.asyncio
    async def test_a_market_settles_once_its_city_day_is_over_not_at_utc_midnight(self, world):
        """16:00 UTC on the 22nd is 01:00 on the 23rd in Seoul: its day is over
        and Polymarket has settled it, though it is still the 22nd in UTC.
        (Fails on the old job, which waited for event_date < the UTC date.)"""
        maker, http, gamma = world
        gamma.winners[SEOUL_SLUG] = "s5"
        await jobs.job_check_resolutions(now=_at(22, 16))

        assert await _markets(maker) == {1: True, 2: False}
        async with maker() as db:
            won = {o.id: o.won for o in (await db.execute(select(MarketOutcome))).scalars().all()}
            opp = (await db.execute(select(Opportunity))).scalars().one()
        assert won == {11: False, 12: True, 13: False, 21: None, 22: None}
        assert (opp.outcome, opp.virtual_status, opp.virtual_pnl) == ("WIN", "win", pytest.approx(1.0))

    @pytest.mark.asyncio
    async def test_a_city_still_inside_its_day_is_not_asked(self, world):
        """At 16:00 UTC it is 08:00 in Los Angeles: no request is spent on it."""
        maker, http, gamma = world
        gamma.winners[SEOUL_SLUG] = "s5"
        await jobs.job_check_resolutions(now=_at(22, 16))
        assert not http.calls_to("los-angeles")

    @pytest.mark.asyncio
    async def test_an_unsettled_market_is_retried_on_the_next_run(self, world):
        maker, http, gamma = world
        await jobs.job_check_resolutions(now=_at(23, 9))        # LA: 01:00 on the 23rd
        assert await _markets(maker) == {1: False, 2: False}
        assert http.calls_to("los-angeles"), "LA's day is over, so it is asked"

        gamma.winners[LA_SLUG] = "l66"
        gamma.winners[SEOUL_SLUG] = "s4"
        await jobs.job_check_resolutions(now=_at(23, 10))
        assert await _markets(maker) == {1: True, 2: True}

    @pytest.mark.asyncio
    async def test_the_real_track_report_shows_the_result(self, world):
        from app.api.admin import admin_realtrack_report
        maker, http, gamma = world
        gamma.winners[SEOUL_SLUG] = "s5"                        # 5°C won: the NO on 5°C lost
        async with maker() as db:
            before = await admin_realtrack_report("t", db, days=365, from_date=DAY, to_date=None, city_id=None)
        await jobs.job_check_resolutions(now=_at(22, 16))
        async with maker() as db:
            after = await admin_realtrack_report("t", db, days=365, from_date=DAY, to_date=None, city_id=None)
        assert before["rows"][0]["result"] == "open"
        (row,) = after["rows"]
        assert row["result"] == "lost" and row["pnl"] == pytest.approx(-4.0)


class TestCost:
    @pytest.mark.asyncio
    async def test_the_model_skill_refresh_stays_about_daily(self, world, monkeypatch):
        """The refresh reads 90 days of forecasts for every city; settlement is
        hourly now, so the refresh is held to once per SKILL_REFRESH_EVERY."""
        import app.analyzers.model_skill as model_skill
        calls = []

        async def fake_update(db):
            calls.append(1)
            return {}
        monkeypatch.setattr(model_skill, "update_model_skill", fake_update)
        maker, http, gamma = world

        gamma.winners[SEOUL_SLUG] = "s5"
        await jobs.job_check_resolutions(now=_at(22, 16))       # Seoul settles
        gamma.winners[LA_SLUG] = "l64"
        await jobs.job_check_resolutions(now=_at(23, 10))       # LA settles 18 h later
        assert len(calls) == 1

        async with maker() as db:                               # another market, 21 h after the refresh
            db.add(Market(id=3, city_id=50, external_id="seoul-23", question="Seoul?",
                          event_date=DAY + timedelta(days=1), resolved=False))
            db.add(MarketOutcome(id=31, market_id=3, bucket_label="7°C", bucket_min=7, bucket_max=7,
                                 bucket_unit="C", token_id="s7"))
            await db.commit()
        gamma.winners["seoul-23"] = "s7"
        await jobs.job_check_resolutions(now=_at(23, 16) + timedelta(hours=1))
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_straggler_is_retried_daily_not_hourly(self, world):
        """A market still open days after its end is checked on the 00 UTC run
        only — or at once from the admin's Resolve pending."""
        maker, http, gamma = world
        async with maker() as db:
            m = await db.get(Market, 2)
            m.event_date = DAY - timedelta(days=5)               # LA, five days ago
            await db.commit()
        gamma.winners[LA_SLUG] = "l64"

        await jobs.job_check_resolutions(now=_at(22, 10))
        assert not http.calls_to("los-angeles") and (await _markets(maker))[2] is False
        await jobs.job_check_resolutions(now=_at(22, 10), include_stragglers=True)
        assert (await _markets(maker))[2] is True

    @pytest.mark.asyncio
    async def test_the_daily_run_takes_the_stragglers(self, world):
        maker, http, gamma = world
        async with maker() as db:
            (await db.get(Market, 2)).event_date = DAY - timedelta(days=5)
            await db.commit()
        gamma.winners[LA_SLUG] = "l64"
        await jobs.job_check_resolutions(now=_at(23, 0) + timedelta(minutes=40))
        assert (await _markets(maker))[2] is True

    def test_resolve_pending_includes_the_stragglers(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "app" / "api" / "admin.py").read_text(encoding="utf-8")
        assert "await job_check_resolutions(include_stragglers=True)" in src

    def test_the_job_is_scheduled_hourly_from_settings(self):
        from pathlib import Path
        from app.config import settings
        assert settings.resolution_check_interval == 3600
        src = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text(encoding="utf-8")
        assert "_add_tracked_job(job_check_resolutions, IntervalTrigger(seconds=settings.resolution_check_interval)" in src

    def test_local_day_over(self):
        assert jobs._local_day_over(DAY, "Asia/Seoul", _at(22, 15))          # 00:00 on the 23rd
        assert not jobs._local_day_over(DAY, "Asia/Seoul", _at(22, 14))
        assert not jobs._local_day_over(DAY, "America/Los_Angeles", _at(23, 7))   # 23:00 on the 22nd
        assert jobs._local_day_over(DAY, "America/Los_Angeles", _at(23, 8))
        assert jobs._local_day_over(DAY, None, _at(23, 0))                   # no zone: UTC
        assert jobs._local_day_over(DAY, "Not/AZone", _at(23, 0))
