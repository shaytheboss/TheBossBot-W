# 04 — Development Plan

Build order is **strictly phased**. Each phase has acceptance criteria. Never start phase N+1 until N's criteria are green.

---

## Phase 0 — Scaffolding (1 PR)

**Branch:** `phase/00-scaffold`

- Create directory layout from `01-ARCHITECTURE.md`.
- `requirements.txt`: `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `apscheduler`, `python-telegram-bot[ext]`, `httpx`, `pydantic-settings`, `python-dateutil`, `pytz`.
- `app/config.py` reads env via `pydantic-settings`.
- `app/main.py` boots FastAPI, prints config-loaded line.
- `Procfile`: `web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- `railway.json` minimal.
- `.env.example`.
- Healthcheck: `GET /health` returns `{"ok": true, "version": "..."}`.

**Acceptance:** Railway deploys, `/health` returns 200.

---

## Phase 1 — DB + station seed (1 PR)

**Branch:** `phase/01-db`

- `app/database.py` with async engine + session factory.
- Alembic init + first migration matching the spec in `02-DATA-MODEL.md`.
- `app/collectors/stations.py` — port `POLYMARKET_STATIONS` from JS to Python `Station` dataclasses. Add `iana_tz` per station (look up from city).
- `app/tools/seed_stations.py` — one-shot upsert.
- Add `python -m app.tools.seed_stations` to Railway start script (idempotent).

**Acceptance:** After deploy, `SELECT count(*) FROM stations` = 20. Each station has a valid IANA tz.

---

## Phase 2 — Telegram skeleton (1 PR)

**Branch:** `phase/02-telegram`

- `app/bot/telegram_bot.py` — build `Application`, register handlers, start polling inside the FastAPI lifespan.
- `app/bot/handlers.py` — `/start`, `/help`, `/status` (just stub counters from DB).
- `app/bot/formatters.py` — `_esc()`, `fmt_help()`, `fmt_status()`.
- Bot must run in the **same process** as FastAPI (one Railway service, not two).

**Acceptance:** Sending `/start` to the bot in Telegram registers the user and replies. `/status` returns "Open: 0, Today: $0, Lifetime: $0". `/help` shows the full command list.

---

## Phase 3 — Open-Meteo ensemble collector (1 PR)

**Branch:** `phase/03-ensemble`

- `app/collectors/open_meteo.py`:
  - `async def fetch_ensemble(station: Station, target_date: date) -> dict` — tries `ecmwf_ifs04 → icon_global → gfs025`, returns members in °C.
- `app/engine/ensemble.py`:
  - `build_distribution(members_c: list[float]) -> Distribution` — port of `buildEnsembleDistribution()`.
  - Computes P10/P25/P50/P75/P90, mean, stddev, 1°C histogram.
- `app/workers/jobs.py:job_refresh_forecasts` — every 1h:
  - For every enabled station × `target_date in (today, +1, +2, +3, +4, +5, +6, +7)`:
    - Fetch each model that the discovery process knows about.
    - Write rows to `forecasts`.
  - Write a `model='consensus'` row using weighted sampling.
- Unit tests in `tests/test_ensemble.py` against a captured fixture from Open-Meteo.

**Acceptance:** After one cycle, `SELECT count(*) FROM forecasts WHERE fetched_at > NOW() - INTERVAL '1 hour'` shows ~20 stations × 8 days × 4 rows (3 models + consensus) = ~640 rows. P50 values look sane (within ±20°C of climate norm).

---

## Phase 4 — METAR collector (1 PR)

**Branch:** `phase/04-metar`

- `app/collectors/metar.py:fetch_metar(icao) -> list[Obs]` — NOAA API `https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=24`.
- Parse `temp` field (already in °C in JSON response).
- `job_fetch_metar` every 15 min, upsert into `metar_obs`.

**Acceptance:** `SELECT count(*) FROM metar_obs WHERE observed_at > NOW() - INTERVAL '6 hours'` shows several rows per station.

---

## Phase 5 — Bias factor (1 PR)

**Branch:** `phase/05-bias`

- `app/engine/bias.py:compute_bias(station, window_days) -> float`:
  - Pull last 14 days of `metar_daily_max` and the corresponding `forecast_p50_c` made at `T-1` for that target date.
  - `bias_c = mean(actual_max - forecast_p50)`.
- `job_refresh_bias` daily at 02:00 UTC. Writes to `station_bias`.
- See **05-BIAS-METHODOLOGY.md** for the math.

**Acceptance:** Run once manually with `python -m app.tools.recompute_bias`. Every station has a row in `station_bias` with `sample_count ≥ 1`.

---

## Phase 6 — Polymarket market discovery (1 PR)

**Branch:** `phase/06-discovery`

- `app/collectors/polymarket.py`:
  - `discover_weather_markets() -> list[GammaMarket]` — paginate Gamma `/events` searching by station city names.
  - Filter: question matches weather regex (port of JS `WEATHER_KEYWORDS`).
- `app/engine/bucket_parser.py:parse_question(text) -> Bucket`:
  - Returns `{ comparator: 'gte'|'lte'|'btw', lo_f, hi_f, unit }`.
  - Port the 9 patterns from the JS code (above/below/between/reach/exceed variants).
  - Returns `None` when uncertain (logged as `unparsed`).
- `app/collectors/polymarket.py:resolve_station(market) -> str | None`:
  - First try wunderground-ICAO regex on `event.description`.
  - Fallback: substring match of station city/alias in `event.title`.
- `job_discover_markets` every 15 min: upsert into `markets`.
- See **06-POLYMARKET-PARSING.md**.

**Acceptance:** After one cycle, `SELECT count(*) FROM markets WHERE status='open'` > 0 for an active week. Spot-check 5 random markets: question, threshold, comparator, station_icao all correct.

---

## Phase 7 — Pricer (1 PR)

**Branch:** `phase/07-pricer`

- `app/engine/pricer.py:price_market(market, forecast, bias_c) -> dict`:
  - Convert market thresholds (°F) → °C.
  - Apply `bias_c` to forecast distribution (shift `members_c` and percentiles by `+bias_c`).
  - Compute bucket probability:
    - `gte`: `1 - Φ((threshold_c - mean_c) / stddev_c)` plus member-based double-check.
    - `lte`: `Φ((threshold_c - mean_c) / stddev_c)`.
    - `btw`: `Φ((hi - mean)/σ) - Φ((lo - mean)/σ)`.
  - Use the **member-based empirical probability as primary**, normal CDF as cross-check (warn if they diverge >5pp).
- Unit tests in `tests/test_pricer.py` with fixture distributions.

**Acceptance:** Given a fixture distribution `mean=27°C, σ=2°C` and a market "above 25°C", the pricer returns ≈0.84.

---

## Phase 8 — Trade engine (1 PR)

**Branch:** `phase/08-trade-engine`

- `app/workers/jobs.py:job_evaluate_trades` every 15 min:
  - For each open market, get latest `consensus` forecast + station bias.
  - For both sides (YES, NO):
    - Compute `model_prob`, `market_prob`.
    - If `model_prob ≥ min_model_prob` AND `model_prob - market_prob ≥ min_edge_pp` AND `enable_trading=true`:
      - Check no existing open trade for `(market, side)`.
      - Insert `trades` row (DB unique index enforces dedup as backup).
      - Snapshot forecast into `trade.forecast_snapshot`.
      - Call `bot.broadcast(fmt_entry(...))`.
      - Insert `alerts_log` row.

**Acceptance:** Set `min_model_prob=0.60` temporarily; verify trades are opened for at least one market. Telegram alert renders correctly. Restore default.

---

## Phase 9 — Resolution tracker (1 PR)

**Branch:** `phase/09-resolution`

- `job_check_resolutions` every 30 min:
  - For each open trade's market, refetch from Gamma:
    - If market `closed` and `outcome` is known:
      - Compute P&L per the formula in `02-DATA-MODEL.md`.
      - Update `trades` row.
      - Update `markets.outcome_yes` and `markets.resolved_at`.
      - `bot.broadcast(fmt_resolution(...))`.

**Acceptance:** Wait for a watched market to resolve (or seed a test market in DB). Trade transitions `open → won/lost`, Telegram alert sent, dashboard reflects.

---

## Phase 10 — Daily summary + heartbeat (1 PR)

**Branch:** `phase/10-summary`

- `job_daily_summary` runs every 15 min, fires when `now.local_hour == user.daily_summary_hour_local AND now.local_minute < 15` AND not already sent today.
  - Per-chat in `telegram_users`: send `fmt_daily_summary`.
  - Insert `alerts_log` row with `alert_type='DAILY'`.
- `job_heartbeat` every 4h to admins only.

**Acceptance:** Manually set `daily_summary_hour_local` to current hour; wait next 15-min cycle; daily summary lands in Telegram. Subsequent cycles in the same hour do NOT re-send (dedup via `alerts_log`).

---

## Phase 11 — Dashboard (1 PR)

**Branch:** `phase/11-dashboard`

- Static HTML/CSS/JS in `app/dashboard/`.
- FastAPI serves at `/`.
- Endpoints:
  - `GET /api/trades?status=&limit=` — list trades.
  - `GET /api/markets?status=open&limit=` — current markets with model price.
  - `GET /api/stats` — daily P&L curve.
  - `GET /api/stations` — stations + bias factor.
  - `GET/PUT /api/settings` — read/write `bot_settings`.
- Pages:
  - `/` — trades table + P&L chart.
  - `/markets` — current markets w/ our prob vs market.
  - `/settings` — threshold + size form (admin token auth).
- See **07-DASHBOARD-SPEC.md**.

**Acceptance:** Visit Railway URL → see trades table, P&L number, settings form. Changing a setting via the form updates `bot_settings` and the next job cycle uses the new value.

---

## Phase 12 — Polish + observability (1 PR)

**Branch:** `phase/12-polish`

- Structured logging (`logging.getLogger`, JSON formatter optional).
- `/api/diag` endpoint that returns connectivity to Polymarket, Open-Meteo, METAR.
- `/diag` Telegram command calls the same.
- Sentry or rollbar integration (optional).
- Pause-on-failure: if 3 consecutive `job_evaluate_trades` runs fail, auto-set `enable_trading=false` and admin alert.
- README pointing at all docs.

**Acceptance:** Full smoke test: deploy, wait one full hour, verify all 7 jobs ran successfully (log lines), one full discovery → eval → entry → resolution cycle complete on a fixture market.

---

## Build-order rationale

This order builds **DB → data → model → trading → alerts → UI**, which ensures:
1. We can always inspect data via SQL before adding logic on top.
2. Telegram bot works at Phase 2 so the loop "deploy → see in Telegram" is short.
3. We don't trade until pricer is tested (Phase 7).
4. Dashboard last because it just visualizes everything already in DB.

## Cadence

Each phase = one branch, one PR. PRs get merged into `main` once acceptance criteria are green. Railway auto-deploys from `main`. **Never merge two phases in one PR.**

## Owner cheat sheet — what to do at the start of each phase

1. `git checkout main && git pull`
2. `git checkout -b phase/NN-name`
3. Code + commit + push.
4. Open PR with checklist of phase acceptance criteria.
5. Test on Railway preview env.
6. Merge to `main`.
7. Update the corresponding section here with "✅ done at commit `<sha>`".
