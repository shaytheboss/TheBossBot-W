"""Memory diagnostics — stdlib only (no psutil).

The Railway RAM bill climbs steadily, which looks like a slow leak in the Python
process (the DB-side retention work did not change it). To fix a leak you must
SEE where it grows, so this module exposes:

  - rss_mb():   resident memory of THIS process, from /proc/self/status.
  - snapshot(): RSS + GC stats + the top object types by instance count + the
                SQLAlchemy pool status + (optional) tracemalloc top allocations.

Calling snapshot() now and again a few hours later shows exactly what is
accumulating (e.g. "500k Forecast objects" or "connection pool exhausted" or a
particular allocation site). Everything here is read-only and cheap.
"""
import gc
import sys
import tracemalloc
from collections import Counter
from typing import Optional


def rss_mb() -> Optional[float]:
    """Resident set size of this process in MB, or None if unavailable."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return round(kb / 1024, 1)
    except Exception:
        pass
    # Fallback: ru_maxrss (KB on Linux) — peak, not current, but better than nothing.
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def top_object_types(limit: int = 25) -> list:
    """Top object types by live instance count (a growing count = the leak)."""
    counts: Counter = Counter()
    for obj in gc.get_objects():
        counts[type(obj).__name__] += 1
    return [{"type": t, "count": n} for t, n in counts.most_common(limit)]


def _pool_status() -> Optional[str]:
    try:
        from app.database import engine
        return engine.pool.status()
    except Exception:
        return None


def snapshot(with_types: bool = True, tracemalloc_limit: int = 15) -> dict:
    """A full, cheap memory snapshot for the admin diagnostic endpoint."""
    gc.collect()
    out: dict = {
        "rss_mb": rss_mb(),
        "gc_counts": gc.get_count(),
        "gc_tracked_objects": len(gc.get_objects()),
        "db_pool": _pool_status(),
        "tracemalloc_enabled": tracemalloc.is_tracing(),
    }
    if with_types:
        out["top_object_types"] = top_object_types()

    if tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot()
        stats = snap.statistics("lineno")[:tracemalloc_limit]
        out["tracemalloc_top"] = [
            {
                "file": f"{st.traceback[0].filename.split('/')[-1]}:{st.traceback[0].lineno}",
                "size_mb": round(st.size / 1024 / 1024, 2),
                "count": st.count,
            }
            for st in stats
        ]
    return out
