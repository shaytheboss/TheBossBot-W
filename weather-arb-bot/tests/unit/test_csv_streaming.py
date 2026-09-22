"""The admin CSV exports must stream, not buffer.

They used to return a `StreamingResponse` wrapping `iter([buf.read()])`, which
streams nothing: the whole export is built in a StringIO and then copied into a
second string, so peak memory holds the ORM objects, the buffer, and the copy
at once. The backtest export is the costly one — `opportunities` averages
~4.4 KB per row because of the `signals` audit trail.

On the Railway memory graph that shows up as a step rather than a spike, since
CPython hands freed blocks back to its own allocator rather than the OS. Memory
is the largest line on the bill (67%), so this is not a cosmetic fix.

The tests below pin the property that matters — memory stays flat as the export
grows — rather than the implementation that currently provides it.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from app.utils.csv_stream import CHUNK_BYTES, _drain, stream_csv, stream_rows

ADMIN = Path(__file__).resolve().parents[2] / "app" / "api" / "admin.py"


async def _collect(response) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


def _rows(n: int, width: int = 200):
    async def source(_session):
        for i in range(n):
            yield {"a": i, "b": "x" * width}
    return source


# ── The response is a real stream ─────────────────────────────────────────

class TestStreaming:
    @pytest.mark.asyncio
    async def test_header_is_emitted_before_any_row_is_read(self):
        """Proof the body is produced lazily: the first chunk arrives without
        the source having been consumed at all."""
        consumed = []

        async def source(_session):
            for i in range(3):
                consumed.append(i)
                yield {"a": i, "b": "y"}

        resp = await stream_csv("t.csv", ["a", "b"], source)
        first = await resp.body_iterator.__anext__()
        assert first.strip() == "a,b"
        assert consumed == [], "rows must not be read to produce the header"

    @pytest.mark.asyncio
    async def test_a_large_export_arrives_in_many_chunks(self):
        resp = await stream_csv("t.csv", ["a", "b"], _rows(2000))
        chunks = [c async for c in resp.body_iterator]
        assert len(chunks) > 5, (
            f"expected the body to be split, got {len(chunks)} chunk(s) — "
            "this is exactly the bug: one chunk means the whole file was built "
            "in memory first"
        )

    @pytest.mark.asyncio
    async def test_no_chunk_is_much_larger_than_the_limit(self):
        """The memory bound. One row can overshoot; a whole file cannot."""
        resp = await stream_csv("t.csv", ["a", "b"], _rows(5000))
        chunks = [c async for c in resp.body_iterator]
        body = "".join(chunks)
        assert len(body) > 4 * CHUNK_BYTES, "test needs an export bigger than one chunk"
        assert max(len(c) for c in chunks) < CHUNK_BYTES * 2

    @pytest.mark.asyncio
    async def test_peak_chunk_size_does_not_grow_with_the_export(self):
        """The property, stated directly: doubling the rows must not double
        the resident buffer."""
        small = [c async for c in (await stream_csv("t.csv", ["a", "b"], _rows(2000))).body_iterator]
        large = [c async for c in (await stream_csv("t.csv", ["a", "b"], _rows(8000))).body_iterator]
        assert len("".join(large)) > 3 * len("".join(small))
        assert max(len(c) for c in large) <= max(len(c) for c in small) * 1.1


# ── The content is still correct ──────────────────────────────────────────

class TestContent:
    @pytest.mark.asyncio
    async def test_every_row_survives_chunking(self):
        """A chunk boundary must never land mid-row and lose data."""
        resp = await stream_csv("t.csv", ["a", "b"], _rows(1500))
        parsed = list(csv.DictReader(io.StringIO(await _collect(resp))))
        assert len(parsed) == 1500
        assert parsed[0]["a"] == "0" and parsed[-1]["a"] == "1499"
        assert all(len(r["b"]) == 200 for r in parsed)

    @pytest.mark.asyncio
    async def test_an_empty_export_is_a_valid_file_with_headers(self):
        async def none(_session):
            return
            yield  # pragma: no cover — makes this an async generator

        body = await _collect(await stream_csv("t.csv", ["a", "b"], none))
        assert body.strip() == "a,b"

    @pytest.mark.asyncio
    async def test_the_download_is_named_and_typed(self):
        resp = await stream_csv("backtest.csv", ["a"], _rows(1))
        assert resp.media_type == "text/csv"
        assert "filename=backtest.csv" in resp.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_a_mid_stream_failure_is_visible_in_the_file(self):
        """Headers are already sent, so the download cannot become a 500.
        Truncating silently would hand over a file that looks complete."""
        async def breaks(_session):
            yield {"a": 1, "b": "ok"}
            raise RuntimeError("database went away")

        body = await _collect(await stream_csv("t.csv", ["a", "b"], breaks))
        assert "1,ok" in body
        assert "EXPORT FAILED" in body and "database went away" in body


# ── Buffer helper ─────────────────────────────────────────────────────────

class TestDrain:
    def test_it_returns_and_clears(self):
        buf = io.StringIO()
        buf.write("hello")
        assert _drain(buf) == "hello"
        assert _drain(buf) == "", "the buffer must not keep what it handed over"
        assert buf.tell() == 0


# ── Fetching rows in batches ──────────────────────────────────────────────

class TestStreamRows:
    @pytest.mark.asyncio
    async def test_it_asks_the_driver_to_stream(self):
        seen = {}

        class _Result:
            def __aiter__(self):
                async def gen():
                    for i in range(3):
                        yield i
                return gen()

        class _DB:
            async def stream(self, q):
                seen["opts"] = q.opts
                return _Result()

        class _Q:
            opts = None

            def execution_options(self, **kw):
                _Q.opts = kw
                return self

        assert [r async for r in stream_rows(_DB(), _Q(), batch=250)] == [0, 1, 2]
        assert seen["opts"] == {"yield_per": 250}

    @pytest.mark.asyncio
    async def test_it_falls_back_when_the_driver_cannot_stream(self):
        """SQLite has no server-side cursor. Tests and any non-Postgres
        deployment must still get their data, just buffered."""
        class _DB:
            async def stream(self, q):
                raise NotImplementedError("no server-side cursor")

            async def execute(self, q):
                class _R:
                    def all(self):
                        return ["a", "b"]
                return _R()

        class _Q:
            def execution_options(self, **kw):
                return self

        assert [r async for r in stream_rows(_DB(), _Q())] == ["a", "b"]


# ── The endpoints actually use it ─────────────────────────────────────────

class TestEndpointsConverted:
    def test_no_export_builds_the_whole_file_first(self):
        """The exact anti-pattern, banned by name so it cannot come back."""
        src = ADMIN.read_text(encoding="utf-8")
        assert "iter([buf.read()])" not in src

    def test_all_three_exports_stream(self):
        src = ADMIN.read_text(encoding="utf-8")
        assert src.count("stream_csv(") >= 3
        for name in ("backtest.csv", "model_skill.csv", "collector_miss.csv"):
            assert f'stream_csv("{name}"' in src

    def test_the_stream_owns_its_session(self):
        """`Depends(get_db)` is closed when the endpoint returns, which for a
        streaming response is before the body is produced — the rows would be
        pulled from a dead session."""
        src = (Path(__file__).resolve().parents[2]
               / "app" / "utils" / "csv_stream.py").read_text(encoding="utf-8")
        assert "AsyncSessionLocal()" in src
        # The module docstring names `Depends(get_db)` to explain why it is
        # avoided, so strip comments and the docstring and check code only.
        code = src.split('"""', 2)[-1]
        code = "\n".join(ln for ln in code.splitlines() if not ln.strip().startswith("#"))
        assert "Depends" not in code
