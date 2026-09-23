"""Per-job CPU and wall-clock accounting.

CPU is 38% of the Railway bill ($4.09 of $14.80 in the current period, at a
steady ~0.39 vCPU). Cutting it means knowing which of the fifteen scheduled
jobs actually burns it, and nothing in the app measured that — the memory
heartbeat had no CPU counterpart.

Wall time and CPU time are both recorded because they answer different
questions. A job that waits on 1,600 HTTP calls has huge wall time and almost
no CPU; a job doing Student-t maths over every bucket has the opposite shape.
Optimising the first means fewer/parallel requests, the second means less
arithmetic. Reading only one number leads to fixing the wrong thing.

A caveat that belongs in the output, not just here: the event loop interleaves,
so the CPU delta measured across a job's await points includes whatever else
ran in that window. Treat per-job CPU as an attribution, not an exact charge.
The process total is exact.
"""
from __future__ import annotations

import logging
import os
import resource
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_PROCESS_START = time.monotonic()


def _cpu_seconds() -> float:
    """User + system CPU consumed by this process so far."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


class _Stat:
    __slots__ = ("runs", "wall", "cpu", "max_wall", "max_cpu", "last_at",
                 "last_wall", "last_cpu", "errors")

    def __init__(self) -> None:
        self.runs = 0
        self.wall = 0.0
        self.cpu = 0.0
        self.max_wall = 0.0
        self.max_cpu = 0.0
        self.last_at: Optional[float] = None
        self.last_wall = 0.0
        self.last_cpu = 0.0
        self.errors = 0


_stats: dict[str, _Stat] = {}


def record(name: str, wall: float, cpu: float, failed: bool = False) -> None:
    st = _stats.setdefault(name, _Stat())
    st.runs += 1
    st.wall += wall
    st.cpu += cpu
    st.max_wall = max(st.max_wall, wall)
    st.max_cpu = max(st.max_cpu, cpu)
    st.last_at = time.time()
    st.last_wall = wall
    st.last_cpu = cpu
    if failed:
        st.errors += 1


def track(name: str, fn: Callable) -> Callable:
    """Wrap a scheduled coroutine so every run is timed.

    The wrapper must never change what the job does: a failure is recorded and
    re-raised exactly as before, so APScheduler's own error handling is
    unaffected.
    """
    async def wrapped(*args, **kwargs) -> Any:
        t0, c0 = time.monotonic(), _cpu_seconds()
        failed = False
        try:
            return await fn(*args, **kwargs)
        except Exception:
            failed = True
            raise
        finally:
            record(name, time.monotonic() - t0, _cpu_seconds() - c0, failed)

    wrapped.__name__ = getattr(fn, "__name__", name)
    wrapped.__doc__ = getattr(fn, "__doc__", None)
    return wrapped


def snapshot() -> dict:
    """Everything measured so far, busiest job first.

    `cpu_pct_of_process` is the share of the process's total CPU attributed to
    each job — the number that says where to look.
    """
    uptime = max(time.monotonic() - _PROCESS_START, 1e-9)
    total_cpu = _cpu_seconds()
    jobs = []
    for name, st in _stats.items():
        jobs.append({
            "job": name,
            "runs": st.runs,
            "cpu_s": round(st.cpu, 2),
            "wall_s": round(st.wall, 2),
            "avg_cpu_ms": round(st.cpu / st.runs * 1000, 1) if st.runs else None,
            "avg_wall_ms": round(st.wall / st.runs * 1000, 1) if st.runs else None,
            "max_wall_ms": round(st.max_wall * 1000, 1),
            "cpu_pct_of_process": round(st.cpu / total_cpu * 100, 1) if total_cpu else None,
            # Share of real time this job was executing. Above ~100% across all
            # jobs means they overlap; near it for one job means it is running
            # essentially all the time.
            "duty_pct": round(st.wall / uptime * 100, 1),
            "errors": st.errors,
            "last_run_s_ago": round(time.time() - st.last_at) if st.last_at else None,
        })
    jobs.sort(key=lambda j: j["cpu_s"], reverse=True)

    measured = sum(j["cpu_s"] for j in jobs)
    return {
        "uptime_s": round(uptime),
        "process_cpu_s": round(total_cpu, 2),
        # The honest headline: total CPU divided by elapsed time is what
        # Railway bills, and it is exact.
        "avg_vcpu": round(total_cpu / uptime, 3),
        "measured_cpu_s": round(measured, 2),
        "unattributed_cpu_s": round(total_cpu - measured, 2),
        "pid": os.getpid(),
        "jobs": jobs,
        "note": (
            "Per-job CPU is an attribution: the event loop interleaves, so a "
            "job's window can include work from other tasks. avg_vcpu is exact."
        ),
    }


def reset() -> None:
    _stats.clear()
