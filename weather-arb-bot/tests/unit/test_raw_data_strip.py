"""Clearing `forecasts.raw_data` once the target date has passed.

Measured 2026-09-22: the database is 3,892 MB, of which `forecasts` is 696 MB
across 267,471 rows — about 2.7 KB per row, where the scalar columns account
for roughly 100 bytes. The rest is `raw_data`: provider payloads, ensemble
member arrays, NWS period objects. Roughly 640 MB, a sixth of the database.

The safety argument is a single claim, and these tests hold it in place:

    `raw_data` has exactly one reader, and it only ever asks for forecast dates
    in the near FUTURE, so a past date's payload can never be read again.

If a second reader appears, or the existing one starts reading history, the
tests in TestNothingElseReadsRawData fail — before the data is gone.
"""
from __future__ import annotations

import inspect
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.workers.retention_job import (
    RAW_DATA_KEEP_DAYS,
    _SUMMARY_KEY_TO_TABLE,
    raw_data_cutoff,
    strip_raw_data,
    vacuum_targets,
)

APP = Path(__file__).resolve().parents[2] / "app"


class _DB:
    """Records UPDATEs and replays a script of rowcounts."""

    def __init__(self, rowcounts):
        self.rowcounts = list(rowcounts)
        self.statements: list[str] = []
        self.params: list[dict] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(params or {})

        class _R:
            rowcount = self.rowcounts.pop(0) if self.rowcounts else 0
        return _R()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


# ── The cutoff ────────────────────────────────────────────────────────────

class TestCutoff:
    def test_default_window_clears_the_trading_horizon_twice_over(self):
        from app.config import settings
        assert RAW_DATA_KEEP_DAYS >= 2 * settings.max_days_ahead_for_alert, (
            "the keep window must comfortably exceed the furthest date the "
            "aggregator can ask for, or a live market loses its ensemble data"
        )

    def test_cutoff_is_in_the_past(self):
        today = date(2026, 9, 22)
        assert raw_data_cutoff(today) == today - timedelta(days=RAW_DATA_KEEP_DAYS)
        assert raw_data_cutoff(today) < today

    def test_settings_can_widen_the_window(self):
        class Cfg:
            raw_data_keep_days = 30
        assert raw_data_cutoff(date(2026, 9, 22), Cfg()) == date(2026, 8, 23)

    def test_a_missing_setting_falls_back_to_the_default(self):
        assert raw_data_cutoff(date(2026, 9, 22), object()) == \
            date(2026, 9, 22) - timedelta(days=RAW_DATA_KEEP_DAYS)


# ── The statement ─────────────────────────────────────────────────────────

class TestStripStatement:
    @pytest.mark.asyncio
    async def test_it_updates_and_never_deletes(self):
        """The whole point: no row leaves the table."""
        db = _DB([5, 0])
        await strip_raw_data(db, date(2026, 9, 15))

        sql = " ".join(db.statements).upper()
        assert "UPDATE FORECASTS" in sql
        assert "SET RAW_DATA = NULL" in sql
        assert "DELETE" not in sql, "this step must never delete a row"
        assert "DROP" not in sql and "TRUNCATE" not in sql

    @pytest.mark.asyncio
    async def test_it_only_touches_past_target_dates(self):
        db = _DB([3, 0])
        cutoff = date(2026, 9, 15)
        await strip_raw_data(db, cutoff)

        sql = " ".join(db.statements).lower()
        assert "forecast_for_date < :cutoff" in sql
        assert db.params[0]["cutoff"] == cutoff

    @pytest.mark.asyncio
    async def test_it_skips_rows_already_cleared(self):
        """Without this the job rewrites the same rows every night, churning
        WAL and dead tuples for no gain."""
        db = _DB([1, 0])
        await strip_raw_data(db, date(2026, 9, 15))
        assert "raw_data is not null" in " ".join(db.statements).lower()

    @pytest.mark.asyncio
    async def test_only_the_forecasts_table_is_touched(self):
        db = _DB([1, 0])
        await strip_raw_data(db, date(2026, 9, 15))
        for other in ("opportunities", "market_prices", "alerts", "virtual_exits"):
            assert other not in " ".join(db.statements).lower()


# ── Batching ──────────────────────────────────────────────────────────────

class TestBatching:
    @pytest.mark.asyncio
    async def test_work_is_split_into_committed_batches(self):
        """One 267k-row UPDATE would spike WAL past the free space on a volume
        already at 85%. Small committed batches keep the peak flat."""
        db = _DB([2000, 2000, 450, 0])
        assert await strip_raw_data(db, date(2026, 9, 15), batch=2000) == 4450
        assert db.commits == 3, "each non-empty batch commits before the next"

    @pytest.mark.asyncio
    async def test_it_stops_as_soon_as_nothing_is_left(self):
        db = _DB([0])
        assert await strip_raw_data(db, date(2026, 9, 15)) == 0
        assert len(db.statements) == 1, "must not keep polling an empty table"
        assert db.commits == 0

    @pytest.mark.asyncio
    async def test_each_statement_is_bounded(self):
        db = _DB([10, 0])
        await strip_raw_data(db, date(2026, 9, 15), batch=10)
        assert "limit :batch" in db.statements[0].lower()
        assert db.params[0]["batch"] == 10

    @pytest.mark.asyncio
    async def test_a_run_cannot_go_on_forever(self):
        """A bug that made every batch report work would otherwise loop until
        the job is killed mid-transaction."""
        db = _DB([5] * 100)
        assert await strip_raw_data(db, date(2026, 9, 15), batch=5, max_batches=3) == 15
        assert len(db.statements) == 3

    @pytest.mark.asyncio
    async def test_a_failure_keeps_the_committed_batches(self):
        """_exec_count swallows the error and returns 0, which ends the loop.
        The rows already cleared stay cleared; the next run resumes."""
        class _Boom(_DB):
            async def execute(self, statement, params=None):
                self.statements.append(str(statement))
                if len(self.statements) == 2:
                    raise RuntimeError("connection lost")
                class _R:
                    rowcount = 100
                return _R()

        db = _Boom([])
        assert await strip_raw_data(db, date(2026, 9, 15)) == 100
        assert db.commits == 1 and db.rollbacks == 1


# ── Wiring ────────────────────────────────────────────────────────────────

class TestWiring:
    def test_stripping_marks_forecasts_for_vacuum(self):
        """Clearing a TOASTed column leaves dead tuples behind. Without a
        vacuum the file never shrinks and the whole exercise frees nothing."""
        assert _SUMMARY_KEY_TO_TABLE["forecasts_raw_data_stripped"] == "forecasts"
        assert "forecasts" in vacuum_targets({"forecasts_raw_data_stripped": 500}, False)

    def test_no_work_means_no_vacuum(self):
        assert vacuum_targets({"forecasts_raw_data_stripped": 0}, False) == []

    def test_it_is_on_by_default(self):
        from app.config import settings
        assert type(settings).model_fields["retention_strip_raw_data_enabled"].default is True

    def test_hard_deletes_stay_off(self):
        """This change must not quietly flip the destructive switch."""
        from app.config import settings
        assert type(settings).model_fields["retention_prune_enabled"].default is False

    @pytest.mark.asyncio
    async def test_the_job_runs_the_step(self, monkeypatch):
        import app.workers.retention_job as rj

        calls = {}

        async def fake_strip(db, cutoff, **kw):
            calls["cutoff"] = cutoff
            return 123

        monkeypatch.setattr(rj, "strip_raw_data", fake_strip)
        monkeypatch.setattr(rj, "AsyncSessionLocal", lambda: _Session())
        monkeypatch.setattr(rj.settings, "retention_dedup_enabled", False)
        monkeypatch.setattr(rj.settings, "retention_prune_enabled", False)
        monkeypatch.setattr(rj.settings, "retention_vacuum_enabled", False)

        summary = await rj.job_prune_old_data()
        assert summary["forecasts_raw_data_stripped"] == 123
        assert calls["cutoff"] < date.today()

    @pytest.mark.asyncio
    async def test_the_step_can_be_switched_off(self, monkeypatch):
        import app.workers.retention_job as rj

        async def fail(*a, **kw):
            raise AssertionError("must not run when disabled")

        monkeypatch.setattr(rj, "strip_raw_data", fail)
        monkeypatch.setattr(rj.settings, "retention_dedup_enabled", False)
        monkeypatch.setattr(rj.settings, "retention_prune_enabled", False)
        monkeypatch.setattr(rj.settings, "retention_strip_raw_data_enabled", False)
        assert await rj.job_prune_old_data() == {}


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **kw):
        class _R:
            rowcount = 0
        return _R()

    async def commit(self):
        return None


# ── The safety claim ──────────────────────────────────────────────────────

class TestNothingElseReadsRawData:
    """`raw_data` may only be read by the aggregator, and only for future dates.

    These are the tests that make the deletion safe. They read the source, so
    a new consumer added anywhere in `app/` fails them.
    """

    @staticmethod
    def _sources_mentioning_raw_data() -> dict[str, list[str]]:
        hits: dict[str, list[str]] = {}
        for path in APP.rglob("*.py"):
            lines = [
                ln.strip()
                for ln in path.read_text(encoding="utf-8").splitlines()
                # Comments explain the column at length; only code counts.
                if "raw_data" in ln and not ln.strip().startswith("#")
            ]
            if lines:
                hits[str(path.relative_to(APP))] = lines
        return hits

    def test_the_reader_set_has_not_grown(self):
        allowed = {
            "models/forecast.py",          # the column definition
            "analyzers/signal_aggregator.py",   # the only reader
            "workers/retention_job.py",    # the writer being added here
            "config.py",                   # raw_data_keep_days
            # Collectors write it on the way in; they never read history.
            "collectors/gfs_collector.py",
            "collectors/hrrr_collector.py",
            "collectors/icon_collector.py",
            "collectors/nws_collector.py",
            "collectors/meteosource_collector.py",
            "collectors/tomorrowio_collector.py",
            "collectors/wunderground_collector.py",
            "collectors/metar_collector.py",
            "analyzers/model_skill.py",    # a comment explaining why it avoids it
        }
        found = set(self._sources_mentioning_raw_data())
        assert found <= allowed, (
            f"new code touches forecasts.raw_data: {sorted(found - allowed)}. "
            "Confirm it never reads a PAST forecast_for_date before allowing it."
        )

    def test_model_skill_still_avoids_the_column(self):
        """PR #101 narrowed this query to four scalar columns precisely so it
        would stop dragging TOAST. If it regresses to select(Forecast) it will
        read raw_data for 90 days of history — which this change empties."""
        src = (APP / "analyzers" / "model_skill.py").read_text(encoding="utf-8")
        # Comments quote `select(Forecast)` to explain what is being avoided,
        # so match code only — the first version of this test failed on the
        # very comment documenting the fix.
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.strip().startswith("#")
        )
        assert "select(Forecast)" not in code
        assert "Forecast.predicted_high_f" in code

    def test_the_aggregator_reads_only_the_date_it_was_given(self):
        """The reader filters on `forecast_for_date == forecast_date`, which
        the detector supplies from `market.event_date` — never a past date for
        an unresolved market."""
        from app.analyzers.signal_aggregator import SignalAggregator
        src = inspect.getsource(SignalAggregator._latest_forecast)
        assert "Forecast.forecast_for_date == forecast_date" in src
        assert not re.search(r"forecast_for_date\s*[<>]", src), (
            "the reader must not scan a date RANGE — a range could reach back "
            "into the window this job clears"
        )

    def test_the_detector_never_analyses_a_past_market(self):
        """The other half of the claim: event_date is always >= today."""
        from app.analyzers.opportunity_detector import detect_opportunities
        src = inspect.getsource(detect_opportunities)
        assert "if days_ahead < 0:" in src and "continue" in src

    def test_scalar_forecast_columns_are_never_touched(self):
        """Accuracy scoring and every screen read these; only the payload goes."""
        src = (APP / "workers" / "retention_job.py").read_text(encoding="utf-8")
        strip = src[src.index("async def strip_raw_data"):src.index("async def job_prune_old_data")]
        for column in ("predicted_high_f", "predicted_low_f", "forecast_for_date",
                       "source", "city_id", "retrieved_at"):
            assert f"{column} =" not in strip, f"{column} must not be modified"
