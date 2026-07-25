"""Memory diagnostics — stdlib only (no psutil).

PERFORMANCE CONTRACT (important):
The Railway RAM bill climbs steadily, which looks like a slow leak in the Python
process. To find it we must measure — but measuring must never disturb trading.

Two clearly separated paths:

  light_snapshot()  — SAFE. Reads /proc, gc counters and the allocator block
                      count. Sub-millisecond, no GC pass, no heap walk. This is
                      what the 15-minute heartbeat and the default endpoint use.

  deep_snapshot()   — EXPENSIVE. Walks the entire heap to census object types.
                      Measured cost: ~100ms per 800k objects → roughly 0.7-1.3s
                      on a leaking multi-GB process, and it holds the GIL for the
                      whole walk (the event loop is frozen: Telegram webhook,
                      Polymarket price fetches and the analyzer all stall).
                      Therefore it is NEVER automatic — opt-in only, on demand,
                      at a moment the operator chooses.

The light path alone answers "is it leaking and how fast" (RSS + allocated
blocks over time). The deep path answers "what exactly is accumulating", and is
needed only once or twice.
"""
import gc
import sys
import tracemalloc
from collections import Counter
from typing import Optional


def rss_mb() -> Optional[float]:
    """Resident set size of this process in MB. Cheap: reads a small /proc file."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(float(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def _pool_status() -> Optional[str]:
    try:
        from app.database import engine
        return engine.pool.status()
    except Exception:
        return None


def light_snapshot() -> dict:
    """Cheap, safe metrics. No GC pass, no heap walk. Sub-millisecond.

    `allocated_blocks` (sys.getallocatedblocks) is an excellent leak proxy: it
    counts live CPython allocator blocks, so a steady climb alongside RSS proves
    accumulation without touching the heap.
    """
    return {
        "mode": "light",
        "rss_mb": rss_mb(),
        "allocated_blocks": sys.getallocatedblocks(),
        "gc_counts": gc.get_count(),          # 3 small ints, cheap
        "gc_collections": [s.get("collections") for s in gc.get_stats()],
        "db_pool": _pool_status(),
        "tracemalloc_enabled": tracemalloc.is_tracing(),
    }


def top_object_types(limit: int = 25) -> list:
    """Top object types by live instance count. EXPENSIVE — full heap walk."""
    counts: Counter = Counter()
    for obj in gc.get_objects():
        counts[type(obj).__name__] += 1
    return [{"type": t, "count": n} for t, n in counts.most_common(limit)]


def deep_snapshot(tracemalloc_limit: int = 15) -> dict:
    """Light metrics + full object-type census (+ tracemalloc if enabled).

    WARNING: freezes the event loop for roughly 0.7-1.3s on a large heap.
    Never call this on a timer — on-demand only.
    """
    out = light_snapshot()
    out["mode"] = "deep"
    objs = gc.get_objects()
    out["gc_tracked_objects"] = len(objs)
    counts: Counter = Counter()
    for obj in objs:
        counts[type(obj).__name__] += 1
    del objs
    out["top_object_types"] = [
        {"type": t, "count": n} for t, n in counts.most_common(25)
    ]

    if tracemalloc.is_tracing():
        stats = tracemalloc.take_snapshot().statistics("lineno")[:tracemalloc_limit]
        out["tracemalloc_top"] = [
            {
                "file": f"{st.traceback[0].filename.split('/')[-1]}:{st.traceback[0].lineno}",
                "size_mb": round(st.size / 1024 / 1024, 2),
                "count": st.count,
            }
            for st in stats
        ]
    return out


def snapshot(deep: bool = False) -> dict:
    """Default is the SAFE light snapshot; deep census strictly opt-in."""
    return deep_snapshot() if deep else light_snapshot()
