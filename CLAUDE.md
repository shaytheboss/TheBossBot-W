# Working agreement

This is a live trading bot. It runs unattended, sends alerts to a real phone,
and costs real money to host. A change that is merely *probably* fine is not
fine.

All commands below run from `weather-arb-bot/`.

---

## The three rules

### 1. Run the tests and the linter after every code change

```bash
cd weather-arb-bot
pytest tests -m "not integration"   # must be 100% green
ruff check app tests                # must be "All checks passed!"
```

Both, every time — not only when the change "looks risky". Report the actual
result. If something fails, fix it and run again before answering; if it
cannot be fixed, say so explicitly rather than reporting success.

Never report a task as finished without having run both in that same session.
"The change is small" is not a reason to skip them; small changes are exactly
where regressions hide.

### 2. Tests must never touch the real network

Enforced in `tests/conftest.py` at the socket layer, not by convention. An
outbound connection from a non-integration test raises `NetworkAccessDenied`.

Register the endpoint on the `http` fixture instead — see
`tests/mocks/http_router.py`. An unregistered URL raises `UnmockedRequest`
naming it, and there is deliberately no fallback response: a default would
let a test pass against a payload nobody wrote.

The same applies to Postgres (`DATABASE_URL` points at an unresolvable host)
and to Telegram (`telegram.Bot` is replaced by `FakeBot`).

The `@pytest.mark.integration` marker is the *only* exemption, it means "hits
the real APIs and spends quota", and those tests are deselected by default.
Do not add that marker to make a failing test pass.

### 3. Every new feature goes through the harness before it is reported done

A feature is finished when it has tests in the harness, not when the code
runs. Concretely:

- New collector or external call → a payload builder in `tests/mocks/`, and a
  test that drives the real collector through it.
- New analyzer logic → a test in `tests/flow/` proving it changes the
  decision, plus the failure case.
- New alert or formatter → assert on `fake_telegram.sent`, with a token set
  (see the trap below).
- Bug fix → a test that fails before the fix and passes after. State that you
  checked it fails before.

---

## The suite

```
tests/
  conftest.py                      fixtures + the three guards
  README.md                        fixture reference — read it before writing tests
  unit/                            fast, isolated (≈460 tests)
    test_harness_selfcheck.py      proves the harness itself works
  flow/                            whole-pipeline tests (≈60 tests)
    conftest.py                    the `pipeline` fixture
    test_full_pipeline.py          happy path, stage by stage
    test_pipeline_edge_cases.py    provider failures, extremes, guards
  integration/                     real APIs, deselected by default
  mocks/                           http_router · weather · polymarket
                                   telegram_fake · db · sqlite_compat
  fixtures/                        cities · scenarios
```

`tests/flow/` runs the real thing: mocked HTTP → real collectors → temporary
SQLite → real detector → real Telegram formatting → `FakeBot`. Nothing
between the edges is stubbed. If you change estimator, detector, collector or
formatter behaviour, expect a flow test to move — and make sure the one that
moves is the one you meant.

Both `tests/flow/` and `tests/unit/` run in the default suite. Neither is
marked `integration`.

---

## Things that will bite you

**A Telegram test without a token asserts nothing.** Every send function opens
with `if not settings.telegram_bot_token: return`. Forget the token and the
test sends nothing, checks nothing, and passes green. Use the `fake_telegram`
fixture, which sets one.

**SQLAlchemy needs `== False`, not `is False`.** `Market.resolved == False`
builds a SQL predicate; `is False` evaluates in Python and silently breaks the
query. E711/E712 are disabled in `ruff.toml` for exactly this reason — do not
"fix" those.

**SQLite is not Postgres.** `tests/mocks/sqlite_compat.py` teaches it `JSONB`,
`ARRAY` and `BigInteger` primary keys. It does not emulate `DISTINCT ON`, and
the raw-SQL bias query (`AT TIME ZONE`, `INTERVAL`) does not parse there — it
falls back to the default +1.5°F prior, which is why `TestBiasCorrection`
injects a bias instead of accumulating one. For Postgres-only SQL use
`FakeSession` or mark the test `integration`.

**Module-level caches leak between tests.** The calibration table, model skill,
model weights and side-alert dedup all memoise at import scope and only
self-clear on a date change. An autouse fixture resets them; do not remove it,
and if you add another cache, add it there.

**Cost is a feature.** This bot runs on Railway and the bill is driven by
Postgres RAM. Before adding a query, a poll or an index, consider what it does
per day: the price collector alone polls every outcome every 5 minutes. Write
on change, select the columns you need, and do not add a `COUNT(*)` to a
dashboard.

---

## Git

Work on `claude/copy-weather-arb-bot-eeRLX`. Do not push to `main`.

Check for an already-open PR before opening a new one. A merged PR cannot
take new commits — branch fresh from `main` instead of stacking onto merged
history.

---

## Honesty

Report what actually happened. If the tests did not run, say they did not run.
If a change did not produce the improvement it was supposed to, say so and say
by how much — a measured non-result is more useful than a confident guess.
Do not claim a number that was not measured.
