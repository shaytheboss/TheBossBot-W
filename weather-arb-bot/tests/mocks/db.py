"""A fake `AsyncSession` for tests that need a database but not Postgres.

`AsyncSessionLocal()` is opened directly in 20+ places (handlers, admin
routes, workers, retention) rather than always flowing through `get_db`, so
there is no single seam to inject a session into. Two tools cover that:

    FakeSession   — hand it to a function that takes `db` as a parameter.
    install(...)  — replace `AsyncSessionLocal` for code that opens its own.

Results are programmed as a queue: each `execute()` pops the next one. That
keeps tests explicit about how many queries they expect a function to run,
which matters here — several of the cost regressions this project has already
fixed were extra queries nobody noticed.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


class FakeResult:
    """Stands in for SQLAlchemy's `Result`, covering the accessors this
    codebase actually uses."""

    def __init__(self, rows: Optional[Iterable[Any]] = None):
        self._rows = list(rows) if rows is not None else []

    # scalar-style access
    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> list:
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError(f"one_or_none() got {len(self._rows)} rows")
        return self.first()

    def scalar_one_or_none(self):
        return self.one_or_none()

    def scalar_one(self):
        if len(self._rows) != 1:
            raise AssertionError(f"scalar_one() got {len(self._rows)} rows")
        return self._rows[0]

    def scalar(self):
        return self.first()

    def fetchall(self) -> list:
        return list(self._rows)

    def fetchone(self):
        return self.first()

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class FakeSession:
    """Records writes, replays programmed reads.

    Anything the code under test `add()`s lands in `.added`, so a test can
    assert on the ORM objects that would have been persisted without a
    database being involved at all.
    """

    def __init__(self, results: Optional[list] = None):
        self._results: list[FakeResult] = [self._coerce(r) for r in (results or [])]
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.executed: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.closed = False
        #: Result handed back once the programmed queue runs dry. Empty by
        #: default so an unanticipated query looks like "no rows" rather than
        #: blowing up in unrelated code.
        self.default_result = FakeResult([])

    @staticmethod
    def _coerce(r) -> FakeResult:
        return r if isinstance(r, FakeResult) else FakeResult(r)

    # ── programming ───────────────────────────────────────────────────────

    def queue(self, *results) -> "FakeSession":
        """Append results, one per upcoming `execute()` call."""
        self._results.extend(self._coerce(r) for r in results)
        return self

    @property
    def pending(self) -> int:
        return len(self._results)

    # ── AsyncSession surface ──────────────────────────────────────────────

    async def execute(self, statement=None, *args, **kwargs) -> FakeResult:
        self.executed.append(statement)
        if self._results:
            return self._results.pop(0)
        return self.default_result

    async def scalar(self, statement=None, *args, **kwargs):
        return (await self.execute(statement)).scalar()

    async def get(self, entity, ident, **kwargs):
        for obj in self.added:
            if isinstance(obj, entity) and getattr(obj, "id", None) == ident:
                return obj
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def add_all(self, objs) -> None:
        self.added.extend(objs)

    async def delete(self, obj) -> None:
        self.deleted.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self, *a, **kw) -> None:
        self.flushes += 1

    async def refresh(self, obj, *a, **kw) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.close()
        return False

    # ── assertions ────────────────────────────────────────────────────────

    def added_of(self, cls) -> list:
        return [o for o in self.added if isinstance(o, cls)]

    def sql(self) -> list[str]:
        """Executed statements as strings, for `"INSERT" in ...` style checks."""
        return [str(s) for s in self.executed]


class SessionFactory:
    """Callable that yields the same `FakeSession` from `async with`.

    Matches how the app uses `AsyncSessionLocal()`, so code that opens its own
    session gets the one the test is holding.
    """

    def __init__(self, session: Optional[FakeSession] = None):
        self.session = session or FakeSession()
        self.opened = 0

    def __call__(self, *a, **kw) -> FakeSession:
        self.opened += 1
        return self.session


def install(monkeypatch, session: Optional[FakeSession] = None, *targets: str) -> FakeSession:
    """Point `AsyncSessionLocal` at a `FakeSession`.

    Because modules do `from app.database import AsyncSessionLocal`, each one
    holds its own reference and patching `app.database` alone is not enough.
    Pass the module paths that need redirecting, e.g.
    `install(mp, sess, "app.workers.jobs", "app.api.admin")`.
    """
    factory = SessionFactory(session)
    monkeypatch.setattr("app.database.AsyncSessionLocal", factory)
    for target in targets:
        monkeypatch.setattr(f"{target}.AsyncSessionLocal", factory, raising=False)
    return factory.session
