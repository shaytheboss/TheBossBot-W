# Test harness

Run the suite:

```bash
pytest tests -m "not integration"     # 461 tests, ~2s, no network, no database
pytest tests -m integration           # hits the real APIs — costs quota
```

## What the harness guarantees

Three things hold for every non-integration test, enforced in `conftest.py`
rather than left to discipline:

| Guarantee | How | Why it is enforced, not trusted |
|---|---|---|
| No network | `socket.connect` blocked for non-loopback addresses | Tomorrow.io's free tier is 500 calls/day. A test that leaks to the real API burns quota and passes only when the internet is up. |
| No Postgres | `DATABASE_URL` points at `db.invalid` | An accidental real connection must fail visibly, not quietly mutate a developer's local database. |
| No Telegram | `telegram.Bot` replaced by `FakeBot` | Nobody's phone should buzz because a test ran. |

A test that calls an endpoint it did not register raises `UnmockedRequest`
naming the URL. There is deliberately **no** default response: a fallback
would let a test pass against a payload nobody wrote.

## Fixtures

| Fixture | Gives you |
|---|---|
| `http` | Empty router. Register routes, assert on `http.calls_to(...)`, `http.count(...)`, `http.param(url, name)`. |
| `weather_api` | `http` with all 8 weather endpoints wired: Austin, 95°F/74°F, sources agreeing. |
| `polymarket_api` | `http` with Gamma + CLOB wired: a 62¢ two-sided market. |
| `fake_telegram` | Records sent messages **and sets a token** (see the trap below). |
| `fake_db` | `FakeSession` to pass as a `db` argument. Records `.added`, replays queued results. |
| `db_factory` | Redirects `AsyncSessionLocal` for code that opens its own session: `db_factory("app.workers.jobs")`. |
| `sqlite_db` | A real in-memory SQLite session, for when the SQL itself is under test. |
| `cities` / `austin` | Seeded cities with real ids, ICAOs and coordinates. |
| `scenarios` | Named signal / market / exit situations. |
| `today` / `now` | Frozen 2026-07-28, so a fixture built on one line can't straddle midnight. |

## Two traps this harness exists to close

**1. Patching `BaseCollector._get` is not enough.** Four places build their
own `httpx.AsyncClient` and never touch it:

```
app/api/admin.py:371,406       app/workers/jobs.py:440,926
app/utils/polymarket_discovery.py    (takes a client as a parameter)
```

The router patches `httpx.AsyncClient.__init__` instead, which is the one
thing all of them share. A caller's own `transport=` argument is discarded on
purpose — no code path can opt out.

**2. A Telegram test without a token asserts nothing.** Every send function
opens with:

```python
if not settings.telegram_bot_token:
    return
```

So a test that forgets the token exercises no formatting, sends no message,
and passes. `fake_telegram` sets a dummy token for exactly this reason;
`test_token_is_empty_by_default` pins the other half, so no test can
accidentally inherit a real token from the environment.

## Postgres-only SQL

`sqlite_db` teaches SQLite three things — `JSONB`, `ARRAY(Integer)`, and
`BigInteger` primary keys (SQLite auto-increments only a column declared
exactly `INTEGER PRIMARY KEY`).

It does **not** emulate `ON CONFLICT DO NOTHING` or `DISTINCT ON`. That is
intentional: those statements are Postgres-specific, and a test that appeared
to exercise them on SQLite would be testing a different query than production
runs. Use `FakeSession` for those, or mark the test `integration`.

## Layout

```
tests/
  conftest.py                      fixtures + the three guards
  unit/test_harness_selfcheck.py   40 tests proving the harness works
  mocks/
    http_router.py                 routing, sequencing, call assertions
    weather_payloads.py            Open-Meteo · Tomorrow.io · Meteosource
                                   NWS · METAR · PIREP · buoy
    polymarket_payloads.py         Gamma · CLOB
    telegram_fake.py               FakeBot
    db.py                          FakeSession · FakeResult
    sqlite_compat.py               in-memory engine + type shims
  fixtures/
    cities.py                      generated from app.utils.seed.CITIES
    scenarios.py                   generated from the estimator's source table
```

`cities.py` and `scenarios.py` derive from application code rather than
copying it, so adding a city or a forecast model updates the fixtures instead
of leaving them quietly stale. `test_harness_selfcheck.py` asserts that link
still holds.
