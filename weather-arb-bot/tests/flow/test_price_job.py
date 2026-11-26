"""The price job and buckets without an order book.

A token whose CLOB order book is gone answers /midpoint with 404 "No orderbook
exists". The job used to ask for it every 5 minutes, logging two ERROR lines
per bucket per run with only the 77-digit token to go on. Now it rests the
bucket for NO_BOOK_RETRY and writes one WARNING naming city, date and bucket.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

import app.workers.jobs as jobs
from app.models.market import Market, MarketOutcome, MarketPrice
from tests.fixtures import cities as city_fixtures
from tests.mocks import polymarket_payloads as pm
from tests.mocks import sqlite_compat

T = datetime(2026, 11, 26, 12, 50, tzinfo=timezone.utc)
LIVE, DEAD = "1111", "2222"


@pytest.fixture
async def world(http, monkeypatch):
    from app.database import Base
    import app.models  # noqa: F401
    engine = sqlite_compat.make_engine()
    await sqlite_compat.create_schema(engine, Base.metadata)
    maker = sqlite_compat.make_sessionmaker(engine)
    monkeypatch.setattr(jobs, "AsyncSessionLocal", maker)
    austin = city_fixtures.get("Austin")
    async with maker() as db:
        db.add(austin.as_row())
        db.add(Market(id=1, city_id=austin.id, external_id="austin-26", question="Austin?",
                      event_date=date(2026, 11, 26), resolved=False))
        db.add(MarketOutcome(id=1, market_id=1, bucket_label="70-71°F", bucket_min=70, bucket_max=71,
                             bucket_unit="F", token_id=LIVE))
        db.add(MarketOutcome(id=2, market_id=1, bucket_label="72-73°F", bucket_min=72, bucket_max=73,
                             bucket_unit="F", token_id=DEAD))
        await db.commit()

    def midpoint(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("token_id") == DEAD:
            return httpx.Response(404, json={"error": "No orderbook exists for the requested token id"})
        return httpx.Response(200, json=pm.midpoint(0.4))
    http.add_handler(pm.CLOB_MIDPOINT, midpoint)
    yield maker, http
    await engine.dispose()


def _asked(http, token: str) -> int:
    return len([r for r in http.calls_to(pm.CLOB_MIDPOINT) if r.url.params.get("token_id") == token])


class TestNoOrderBook:
    @pytest.mark.asyncio
    async def test_one_warning_names_the_bucket_and_no_error_is_logged(self, world, caplog):
        maker, http = world
        caplog.set_level(logging.DEBUG)
        await jobs.job_fetch_polymarket(now=T)

        async with maker() as db:
            prices = (await db.execute(select(MarketPrice.outcome_id))).scalars().all()
        assert prices == [1], "the live bucket is still priced"
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
            [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        # (SQLite also warns that the Postgres-only DISTINCT ON lookup fell back.)
        (w,) = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "order book" in r.getMessage()]
        assert "1 outcomes have no order book" in w and "Austin 2026-11-26 72-73°F" in w
        assert "asked again in 60 min" in w

    @pytest.mark.asyncio
    async def test_the_bucket_rests_for_an_hour_then_is_asked_again(self, world, caplog):
        maker, http = world
        await jobs.job_fetch_polymarket(now=T)
        await jobs.job_fetch_polymarket(now=T + timedelta(minutes=5))
        await jobs.job_fetch_polymarket(now=T + timedelta(minutes=55))
        assert (_asked(http, DEAD), _asked(http, LIVE)) == (1, 3)

        caplog.set_level(logging.INFO)
        await jobs.job_fetch_polymarket(now=T + jobs.NO_BOOK_RETRY + timedelta(minutes=1))
        assert _asked(http, DEAD) == 2
        assert any("polling 2 of 2 outcomes" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_polling_line_says_how_many_rest(self, world, caplog):
        maker, http = world
        await jobs.job_fetch_polymarket(now=T)
        caplog.set_level(logging.INFO)
        await jobs.job_fetch_polymarket(now=T + timedelta(minutes=5))
        assert any("polling 1 of 2 outcomes (0 beyond the 3-day horizon deferred, 1 without an order book resting)"
                   in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_other_errors_still_log_as_errors(self, world, caplog):
        """Only 'no order book' is quiet: a server error is still an ERROR."""
        maker, http = world
        http.prepend_handler(pm.CLOB_MIDPOINT, lambda r: httpx.Response(400, json={"error": "bad"}))
        caplog.set_level(logging.DEBUG)
        await jobs.job_fetch_polymarket(now=T)
        assert [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not jobs._no_book_until, "a 400 is not 'no order book': nothing rests"
