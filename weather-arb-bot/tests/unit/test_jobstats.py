"""Per-job CPU accounting.

CPU is 38% of the Railway bill ($4.09 of $14.80) at a steady ~0.39 vCPU, and
nothing measured which of the fifteen scheduled jobs spends it. Guessing at
that from the outside is how the earlier cost work went wrong twice.

The one hard requirement: instrumentation must not change behaviour. A wrapped
job returns exactly what it returned, raises exactly what it raised, and costs
nothing measurable.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.utils import jobstats

MAIN = Path(__file__).resolve().parents[2] / "app" / "main.py"


@pytest.fixture(autouse=True)
def _clean():
    jobstats.reset()
    yield
    jobstats.reset()


# ── The wrapper is transparent ────────────────────────────────────────────

class TestTransparency:
    @pytest.mark.asyncio
    async def test_the_return_value_is_untouched(self):
        async def job():
            return {"written": 7}
        assert await jobstats.track("t", job)() == {"written": 7}

    @pytest.mark.asyncio
    async def test_arguments_pass_through(self):
        async def job(a, b=2):
            return a + b
        assert await jobstats.track("t", job)(1, b=5) == 6

    @pytest.mark.asyncio
    async def test_an_exception_propagates_unchanged(self):
        """APScheduler's own error handling must still see the real error."""
        class Boom(RuntimeError):
            pass

        async def job():
            raise Boom("original message")

        with pytest.raises(Boom, match="original message"):
            await jobstats.track("t", job)()

    @pytest.mark.asyncio
    async def test_a_failed_run_is_still_counted(self):
        async def job():
            raise ValueError("nope")
        with pytest.raises(ValueError):
            await jobstats.track("t", job)()
        st = jobstats.snapshot()["jobs"][0]
        assert st["runs"] == 1 and st["errors"] == 1

    @pytest.mark.asyncio
    async def test_the_job_keeps_its_name(self):
        """APScheduler logs by function name; an opaque wrapper makes every
        log line say the same thing."""
        async def job_fetch_polymarket():
            return None
        assert jobstats.track("polymarket", job_fetch_polymarket).__name__ == \
            "job_fetch_polymarket"


# ── The numbers mean something ────────────────────────────────────────────

class TestMeasurement:
    @pytest.mark.asyncio
    async def test_waiting_costs_wall_time_but_not_cpu(self):
        """The distinction the whole module exists for. A job that waits on
        HTTP must not look like a job that burns CPU."""
        async def waits():
            await asyncio.sleep(0.05)

        await jobstats.track("io", waits)()
        st = jobstats.snapshot()["jobs"][0]
        assert st["wall_s"] >= 0.04
        assert st["cpu_s"] < 0.02, "sleeping must not be charged as CPU"

    @pytest.mark.asyncio
    async def test_computing_costs_cpu(self):
        async def burns():
            t = time.monotonic()
            x = 0
            while time.monotonic() - t < 0.05:
                x += 1
            return x

        await jobstats.track("cpu", burns)()
        st = jobstats.snapshot()["jobs"][0]
        assert st["cpu_s"] >= 0.02, "a busy loop must show up as CPU"

    @pytest.mark.asyncio
    async def test_runs_accumulate(self):
        # 25 ms, not 1 ms: wall_s is reported to two decimals, so four
        # millisecond runs round to 0.00 and the average cannot be checked
        # against it.
        async def job():
            await asyncio.sleep(0.025)
        wrapped = jobstats.track("t", job)
        for _ in range(4):
            await wrapped()
        st = jobstats.snapshot()["jobs"][0]
        assert st["runs"] == 4
        assert st["wall_s"] >= 0.09
        assert st["avg_wall_ms"] == pytest.approx(st["wall_s"] * 1000 / 4, rel=0.05)

    @pytest.mark.asyncio
    async def test_the_busiest_job_is_listed_first(self):
        """The report exists to answer "where is it going" at a glance."""
        async def light():
            await asyncio.sleep(0.001)

        async def heavy():
            t = time.monotonic()
            while time.monotonic() - t < 0.05:
                pass

        await jobstats.track("light", light)()
        await jobstats.track("heavy", heavy)()
        assert jobstats.snapshot()["jobs"][0]["job"] == "heavy"


# ── The report is honest ──────────────────────────────────────────────────

class TestSnapshot:
    def test_it_reports_the_exact_billed_figure(self):
        """Per-job CPU is an attribution — the event loop interleaves. The
        process average is not, and it is what Railway charges for."""
        s = jobstats.snapshot()
        assert s["avg_vcpu"] >= 0
        assert "attribution" in s["note"]

    @pytest.mark.asyncio
    async def test_unattributed_cpu_is_shown_not_hidden(self):
        """The web server, the Telegram webhook and pool upkeep run outside
        every job. If that is where the CPU goes, the job table would look
        innocent and the real answer would be invisible."""
        async def job():
            await asyncio.sleep(0.001)
        await jobstats.track("t", job)()

        s = jobstats.snapshot()
        assert "unattributed_cpu_s" in s
        assert s["unattributed_cpu_s"] == pytest.approx(
            s["process_cpu_s"] - s["measured_cpu_s"], abs=0.01
        )

    def test_an_empty_snapshot_is_valid(self):
        s = jobstats.snapshot()
        assert s["jobs"] == [] and s["uptime_s"] >= 0

    @pytest.mark.asyncio
    async def test_reset_clears_the_counters(self):
        async def job():
            return None
        await jobstats.track("t", job)()
        assert jobstats.snapshot()["jobs"]
        jobstats.reset()
        assert jobstats.snapshot()["jobs"] == []


# ── Every scheduled job is actually wrapped ───────────────────────────────

class TestEveryJobIsTracked:
    def test_no_job_bypasses_the_wrapper(self):
        """A job registered with the raw scheduler would be invisible, and an
        invisible job is exactly the one that turns out to be the expensive
        one. Only the helper itself may call add_job."""
        src = MAIN.read_text(encoding="utf-8")
        raw = [
            ln.strip() for ln in src.splitlines()
            if "_scheduler.add_job(" in ln and "def _add_tracked_job" not in ln
        ]
        assert len(raw) == 1 and "track(id, func)" in raw[0], (
            f"unwrapped scheduler registration(s): {raw}"
        )

    def test_the_jobs_are_named_by_their_scheduler_id(self):
        """Names have to match the ids in main.py or the report reads as
        gibberish."""
        src = MAIN.read_text(encoding="utf-8")
        assert 'id="polymarket"' in src and 'id="analyzer"' in src
        assert "_add_tracked_job(job_fetch_polymarket" in src
        assert "_add_tracked_job(job_run_analyzer" in src

    def test_the_expensive_suspects_are_covered(self):
        """The price poll and the analyzer both run every 5 minutes and are
        the two candidates for a steady 0.39 vCPU."""
        src = MAIN.read_text(encoding="utf-8")
        for job in ("job_fetch_polymarket", "job_run_analyzer",
                    "job_run_intraday", "job_fetch_models"):
            assert f"_add_tracked_job({job}" in src, f"{job} is not tracked"
