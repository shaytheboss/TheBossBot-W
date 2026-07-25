"""Tests for the database size diagnostic.

The queries are Postgres-specific (catalog views), so the contract under test is:
formatting is correct, and a non-Postgres / failing engine degrades gracefully
into an `error` field instead of raising into the request handler.
"""
import pytest

from app.utils import dbdiag


class _FailingDB:
    async def execute(self, *a, **kw):
        raise RuntimeError("no such function: pg_database_size")


class _Row:
    def __init__(self, name, total, tbl, idx, live, dead):
        self.table_name = name
        self.total_bytes = total
        self.table_bytes = tbl
        self.index_bytes = idx
        self.live_rows = live
        self.dead_rows = dead
        self.last_vacuum = None
        self.last_autovacuum = None


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeDB:
    """First execute() → total size; second → table rows."""
    def __init__(self, total, rows):
        self._total = total
        self._rows = rows
        self._calls = 0

    async def execute(self, *a, **kw):
        self._calls += 1
        if self._calls == 1:
            return _FakeResult(scalar=self._total)
        return _FakeResult(rows=self._rows)


def test_mb_conversion():
    assert dbdiag._mb(1024 * 1024) == 1.0
    assert dbdiag._mb(int(2.5 * 1024 * 1024)) == 2.5
    assert dbdiag._mb(None) is None


@pytest.mark.asyncio
async def test_failing_engine_returns_error_not_raise():
    out = await dbdiag.database_size(_FailingDB())
    assert "error" in out
    assert "total_mb" not in out


@pytest.mark.asyncio
async def test_reports_total_and_tables():
    rows = [
        _Row("forecasts", 3 * 1024**3, 2 * 1024**3, 1024**3, 5_000_000, 2_000_000),
        _Row("market_prices", 512 * 1024**2, 400 * 1024**2, 112 * 1024**2, 900_000, 1_000),
    ]
    out = await dbdiag.database_size(_FakeDB(4 * 1024**3, rows))
    assert out["total_mb"] == 4096.0
    assert len(out["tables"]) == 2
    t0 = out["tables"][0]
    assert t0["table"] == "forecasts"
    assert t0["total_mb"] == 3072.0
    assert t0["live_rows"] == 5_000_000


@pytest.mark.asyncio
async def test_dead_rows_drive_vacuum_recommendation():
    heavy = [_Row("forecasts", 1024**3, 1024**3, 0, 100, 500_000)]
    out = await dbdiag.database_size(_FakeDB(1024**3, heavy))
    assert out["dead_rows_total"] == 500_000
    assert out["vacuum_full_recommended"] is True


@pytest.mark.asyncio
async def test_clean_db_does_not_recommend_vacuum():
    light = [_Row("forecasts", 1024**2, 1024**2, 0, 1000, 5)]
    out = await dbdiag.database_size(_FakeDB(1024**2, light))
    assert out["vacuum_full_recommended"] is False
    assert out["bloated_tables"] == []


class _CountingDB(_FakeDB):
    """Adds COUNT(*) responses after the size + table queries."""
    def __init__(self, total, rows, counts):
        super().__init__(total, rows)
        self._counts = list(counts)

    async def execute(self, *a, **kw):
        self._calls += 1
        if self._calls == 1:
            return _FakeResult(scalar=self._total)
        if self._calls == 2:
            return _FakeResult(rows=self._rows)
        return _FakeResult(scalar=self._counts.pop(0))


@pytest.mark.asyncio
async def test_estimates_alone_never_declare_bloat():
    """REGRESSION: production reported market_prices at 3,561 live rows while
    COUNT(*) said 17,532,604. Dividing size by that estimate produced a bogus
    ~872 KB/row and a false BLOATED alarm on a table full of real data.
    Estimates must never drive the verdict."""
    rows = [_Row("market_prices", 2963 * 1024**2, 1144 * 1024**2, 1819 * 1024**2, 3_561, 0)]
    out = await dbdiag.database_size(_FakeDB(3871 * 1024**2, rows))
    assert out["tables"][0]["bloated"] is False
    assert out["bloated_tables"] == []
    assert out["tables"][0]["bytes_per_row_is_estimate"] is True


@pytest.mark.asyncio
async def test_exact_counts_show_table_is_healthy():
    """With the true 17.5M count, bytes-per-row is ~177 — perfectly normal."""
    rows = [_Row("market_prices", 2963 * 1024**2, 1144 * 1024**2, 1819 * 1024**2, 3_561, 0)]
    out = await dbdiag.database_size(
        _CountingDB(3871 * 1024**2, rows, [17_532_604]), exact_counts=True
    )
    t = out["tables"][0]
    assert t["exact_rows"] == 17_532_604
    assert t["bytes_per_row"] < 500, "real bytes/row must be sane"
    assert t["bytes_per_row_is_estimate"] is False
    assert t["bloated"] is False
    assert out["vacuum_full_recommended"] is False


@pytest.mark.asyncio
async def test_exact_counts_detect_genuine_bloat():
    """A table truly holding almost nothing while occupying GB IS bloated."""
    rows = [_Row("market_prices", 2963 * 1024**2, 1144 * 1024**2, 1819 * 1024**2, 3_561, 0)]
    out = await dbdiag.database_size(
        _CountingDB(3871 * 1024**2, rows, [12]), exact_counts=True
    )
    assert out["tables"][0]["bloated"] is True
    assert out["vacuum_full_recommended"] is True


@pytest.mark.asyncio
async def test_small_tables_never_flagged():
    """Tiny tables can have odd ratios; they are irrelevant to cost."""
    rows = [_Row("app_settings", 1024, 1024, 0, 0, 0)]
    out = await dbdiag.database_size(_CountingDB(1024, rows, [0]), exact_counts=True)
    assert out["tables"][0]["bloated"] is False
