"""Tests for the memory diagnostic helpers.

The critical property under test is the PERFORMANCE CONTRACT: the light path
(used by the automatic heartbeat and the default endpoint) must never walk the
heap or force a GC pass, because that holds the GIL for ~1s on a large heap and
would stall the webhook / price fetches / analyzer.
"""
import gc
import time

from app.utils import memdiag


# ── Cheap path ────────────────────────────────────────────────────────────────

def test_rss_mb_returns_number():
    v = memdiag.rss_mb()
    assert v is None or (isinstance(v, float) and v > 0)


def test_light_snapshot_has_core_fields():
    s = memdiag.light_snapshot()
    for k in ("mode", "rss_mb", "allocated_blocks", "gc_counts", "db_pool"):
        assert k in s
    assert s["mode"] == "light"
    assert isinstance(s["allocated_blocks"], int) and s["allocated_blocks"] > 0


def test_light_snapshot_does_not_census():
    """The light path must NOT include heap-walk-derived fields."""
    s = memdiag.light_snapshot()
    assert "top_object_types" not in s
    assert "gc_tracked_objects" not in s


def test_light_snapshot_does_not_run_gc():
    """A forced collection would reset the GC counters — assert it doesn't."""
    before = gc.get_stats()[0].get("collections")
    memdiag.light_snapshot()
    after = gc.get_stats()[0].get("collections")
    assert after == before, "light_snapshot must not trigger a gc pass"


def test_light_snapshot_is_fast():
    """Must be far below any latency that could disturb the event loop."""
    t = time.perf_counter()
    for _ in range(20):
        memdiag.light_snapshot()
    elapsed_ms = (time.perf_counter() - t) * 1000 / 20
    assert elapsed_ms < 20, f"light_snapshot too slow: {elapsed_ms:.1f} ms"


def test_snapshot_defaults_to_light():
    """Default MUST be the safe path — a deep default would freeze the bot."""
    assert memdiag.snapshot()["mode"] == "light"
    assert "top_object_types" not in memdiag.snapshot()


# ── Expensive path (opt-in) ───────────────────────────────────────────────────

def test_deep_snapshot_includes_census():
    s = memdiag.snapshot(deep=True)
    assert s["mode"] == "deep"
    assert isinstance(s["gc_tracked_objects"], int)
    top = s["top_object_types"]
    assert isinstance(top, list) and top
    for e in top:
        assert set(e) == {"type", "count"}


def test_deep_snapshot_census_sorted_desc():
    top = memdiag.snapshot(deep=True)["top_object_types"]
    counts = [e["count"] for e in top]
    assert counts == sorted(counts, reverse=True)


def test_deep_snapshot_superset_of_light():
    light, deep = memdiag.light_snapshot(), memdiag.snapshot(deep=True)
    for k in light:
        if k != "mode":
            assert k in deep
