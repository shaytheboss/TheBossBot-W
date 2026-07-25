"""Tests for the memory diagnostic helpers (leak-hunting instrumentation)."""
from app.utils import memdiag


def test_rss_mb_returns_number():
    v = memdiag.rss_mb()
    assert v is None or (isinstance(v, float) and v > 0)


def test_top_object_types_shape():
    top = memdiag.top_object_types(limit=5)
    assert isinstance(top, list) and len(top) <= 5
    for entry in top:
        assert set(entry) == {"type", "count"}
        assert isinstance(entry["count"], int) and entry["count"] > 0


def test_top_object_types_sorted_desc():
    top = memdiag.top_object_types(limit=10)
    counts = [e["count"] for e in top]
    assert counts == sorted(counts, reverse=True)


def test_snapshot_has_core_fields():
    snap = memdiag.snapshot(with_types=True)
    for k in ("rss_mb", "gc_counts", "gc_tracked_objects", "top_object_types", "tracemalloc_enabled"):
        assert k in snap
    assert isinstance(snap["gc_tracked_objects"], int)


def test_snapshot_without_types_is_light():
    snap = memdiag.snapshot(with_types=False)
    assert "top_object_types" not in snap
    assert "rss_mb" in snap
