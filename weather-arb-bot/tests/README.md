# Test harness

Run the suite:

```bash
pytest tests -m "not integration"     # 517 tests, ~10s, no network, no database
pytest tests -m integration           # hits the real APIs — costs quota
ruff check app tests                  # must be clean before committing
```

Two layers:

- **`tests/unit/`** — fast and isolated. Most tests belong here.
- **`tests/flow/`** — the whole pipeline end to end: mocked HTTP → real
  collectors → temporary SQLite → real detector → real Telegram formatting →
  `FakeBot`. Nothing between the edges is stubbed.

Neither is marked `integration`; both run on every commit.

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

## The flow suite

`tests/flow/` seeds one city and one market — Austin, resolving tomorrow, four
2°F buckets from 91 to 98, every source reporting 95°F, a 62¢ two-sided book —
and exposes the four real stages:

```python
async def test_something(pipeline):
    await pipeline.collect_forecasts()   # 5 collectors → forecasts table
    await pipeline.collect_ensemble()
    await pipeline.collect_prices()      # Polymarket  → market_prices
    result = await pipeline.detect()     # the real detector
    await pipeline.alert(result)         # the real send functions → FakeBot

    assert result.bucket_sides() == {"93-94°F": "NO"}
    assert "Austin" in pipeline.telegram.only().text
```

`pipeline.run()` does all four. Shape the world with the indirect fixtures:

| Fixture | Default | Example |
|---|---|---|
| `forecast_temp` | 95.0 | `@pytest.mark.parametrize("forecast_temp", [130.0], indirect=True)` |
| `source_spread` | 0.0 | fan the sources apart in °F |
| `days_ahead` | 1 | `[4]` puts the market past the 3-day horizon |
| `market_buckets` | 91-98 | a different bucket ladder |

Break things with `break_source(pipeline.http, endpoint, status=503)`, or
`pipeline.http.prepend(...)` for a custom response. Use `no_backoff` whenever
a test triggers a retry, or it spends 14 seconds sleeping.

### Writing a flow test that is worth having

Two failure modes to avoid, both of which produce a green test that checks
nothing:

**Asserting absence without a control.** "No opportunity was created" can mean
the guard worked — or that the data never arrived. Pair it with a control that
*does* fire. `test_one_day_past_the_horizon_is_skipped` sits next to
`test_the_last_day_inside_the_horizon_still_trades` for this reason; without
the control it passed even with the horizon check deleted.

**Assuming a suppressed signal was computed.** Where a threshold blocks a
trade, relax the threshold and show the signal appear — that is what proves
the value reached the estimator instead of being swallowed by an exception.
See `test_the_extreme_value_did_reach_the_estimator`.

The suite was checked against nine deliberate mutations — write-on-change,
horizon guard, normalisation, sparse-source shrink, blacklist, Markdown
dialect, METAR °C→°F, retry, bias correction — and each one turned it red.

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
  flow/
    conftest.py                    the `pipeline` fixture
    test_full_pipeline.py          happy path, stage by stage
    test_pipeline_edge_cases.py    provider failures, extremes, guards
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
