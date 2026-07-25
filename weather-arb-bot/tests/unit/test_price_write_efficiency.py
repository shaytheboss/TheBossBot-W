"""Tests for the market_prices write-on-change optimisation.

Polling every 5 minutes and writing unconditionally produced ~290,000 rows/day
(17.5M rows / ~3 GB) even though most outcomes do not move between polls. The
collector now writes only when the price actually changes, with an hourly
heartbeat so a long flat stretch is still represented and never becomes a gap.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.collectors.polymarket_collector import PolymarketCollector


class _DB:
    """Records execute/commit calls."""
    def __init__(self):
        self.executes = 0
        self.commits = 0

    async def execute(self, *a, **kw):
        self.executes += 1

    async def commit(self):
        self.commits += 1


@pytest.fixture
def col(monkeypatch):
    c = PolymarketCollector()

    async def fake_mid(token_id):
        return fake_mid.value
    fake_mid.value = 0.62
    monkeypatch.setattr(c, "get_midpoint", fake_mid)
    return c


NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_unchanged_recent_price_is_not_written(col):
    db = _DB()
    res = await col.collect_and_store(1, "tok", db, last=(0.62, NOW - timedelta(minutes=5)))
    assert res["skipped"] is True
    assert db.executes == 0, "an unchanged price must not hit the database"


@pytest.mark.asyncio
async def test_changed_price_is_written(col):
    db = _DB()
    res = await col.collect_and_store(1, "tok", db, last=(0.55, NOW - timedelta(minutes=5)))
    assert res["skipped"] is False
    assert db.executes == 1, "a real price change must always be recorded"


@pytest.mark.asyncio
async def test_tiny_change_still_counts_as_a_change(col):
    """Lossless: any movement at all is stored, never rounded away."""
    db = _DB()
    res = await col.collect_and_store(1, "tok", db, last=(0.6201, NOW - timedelta(minutes=5)))
    assert res["skipped"] is False
    assert db.executes == 1


@pytest.mark.asyncio
async def test_heartbeat_writes_even_when_unchanged(col):
    """After the heartbeat interval a flat price is written anyway, so the
    series keeps at least hourly coverage instead of an ambiguous gap."""
    db = _DB()
    res = await col.collect_and_store(
        1, "tok", db, last=(0.62, NOW - timedelta(hours=2)), heartbeat_seconds=3600
    )
    assert res["skipped"] is False
    assert db.executes == 1


@pytest.mark.asyncio
async def test_first_ever_price_is_written(col):
    db = _DB()
    res = await col.collect_and_store(1, "tok", db, last=None)
    assert res["skipped"] is False
    assert db.executes == 1


@pytest.mark.asyncio
async def test_commit_false_defers_to_caller(col):
    """The job batches one commit for all outcomes instead of one per outcome."""
    db = _DB()
    await col.collect_and_store(1, "tok", db, last=None, commit=False)
    assert db.executes == 1
    assert db.commits == 0


@pytest.mark.asyncio
async def test_commit_true_commits(col):
    db = _DB()
    await col.collect_and_store(1, "tok", db, last=None, commit=True)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_missing_midpoint_writes_nothing(col, monkeypatch):
    async def none_mid(token_id):
        return None
    monkeypatch.setattr(col, "get_midpoint", none_mid)
    db = _DB()
    assert await col.collect_and_store(1, "tok", db, last=None) is None
    assert db.executes == 0
