"""Streaming CSV responses that do not hold the whole export in memory.

The admin exports returned a `StreamingResponse` but did not stream. Each one:

    rows = (await db.execute(q)).all()   # every ORM object, all at once
    ...build the entire file in a StringIO...
    return StreamingResponse(iter([buf.read()]), ...)

`buf.read()` copies the finished file into a second string, so at peak the
process holds the ORM objects, the buffer, and the copy simultaneously. The
backtest export is the expensive one: `opportunities` averages ~4.4 KB per row
(the `signals` JSONB audit trail), so a 90-day export materialises tens of MB
of payload and several times that in Python object overhead.

That shows up on the Railway memory graph as a step, not a spike — CPython
returns freed blocks to its own allocator, not always to the OS, so the RSS
high-water mark tends to stay.

This module fixes the shape: rows arrive from the database in batches, each row
is written and flushed, and nothing larger than one chunk is ever resident.

Session ownership
-----------------
The generator opens its OWN session instead of taking one from
`Depends(get_db)`. FastAPI closes a yield-dependency when the endpoint returns,
which for a streaming response is *before* the body has been produced — the
rows would be pulled from a closed session. Owning the session here keeps it
alive exactly as long as the stream.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import AsyncIterator, Callable, Iterable

from fastapi.responses import StreamingResponse

from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

#: Flush once the pending text passes this size. Big enough that the per-chunk
#: overhead is irrelevant, small enough that memory stays flat on any export.
CHUNK_BYTES = 64 * 1024

#: Rows fetched from the server per round trip. Streams the result set instead
#: of buffering it client-side.
DB_BATCH = 500


def _drain(buf: io.StringIO) -> str:
    """Take everything written so far and reset the buffer."""
    text = buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    return text


async def stream_csv(
    filename: str,
    fieldnames: Iterable[str],
    row_source: Callable[[object], AsyncIterator[dict]],
    *,
    chunk_bytes: int = CHUNK_BYTES,
) -> StreamingResponse:
    """Build a CSV download that streams.

    `row_source` is called with a live `AsyncSession` and must be an async
    generator yielding one dict per CSV row.
    """
    fields = list(fieldnames)

    async def generate() -> AsyncIterator[str]:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        yield _drain(buf)
        try:
            async with AsyncSessionLocal() as db:
                async for row in row_source(db):
                    writer.writerow(row)
                    if buf.tell() >= chunk_bytes:
                        yield _drain(buf)
            tail = _drain(buf)
            if tail:
                yield tail
        except Exception as exc:
            # The status line and headers are already on the wire, so the
            # download cannot be turned into a 500. Emit a final row the reader
            # will notice instead of truncating the file silently.
            logger.error(f"[csv_stream] {filename} failed mid-stream: {exc}", exc_info=True)
            yield _drain(buf)
            yield f"\n# EXPORT FAILED: {type(exc).__name__}: {exc}\n"

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


async def stream_rows(db, query, batch: int = DB_BATCH):
    """Yield result rows from `query` without materialising the whole set.

    Falls back to a plain buffered execute when the driver cannot stream — SQLite
    under aiosqlite has no server-side cursor — so tests and any non-Postgres
    deployment still work, just without the memory benefit.
    """
    try:
        result = await db.stream(query.execution_options(yield_per=batch))
    except Exception as exc:
        logger.debug(f"[csv_stream] server-side cursor unavailable ({exc}); buffering")
        for row in (await db.execute(query)).all():
            yield row
        return
    async for row in result:
        yield row
