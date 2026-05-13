"""In-memory ring buffer for the most recent log records, used by the admin UI."""
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Deque

_BUFFER_SIZE = 500
_buffer: Deque[dict] = deque(maxlen=_BUFFER_SIZE)


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        _buffer.append({
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": msg,
        })


def install_buffer_handler() -> None:
    root = logging.getLogger()
    if any(isinstance(h, RingBufferHandler) for h in root.handlers):
        return
    handler = RingBufferHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)


def recent_logs(limit: int = 200, level: str | None = None) -> list[dict]:
    items = list(_buffer)
    if level:
        level_up = level.upper()
        items = [i for i in items if i["level"] == level_up]
    return items[-limit:][::-1]
