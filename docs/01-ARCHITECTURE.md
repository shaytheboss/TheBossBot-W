# 01 — System Architecture

## Goal

A Telegram bot + web dashboard that automatically:

1. Discovers active **US weather temperature markets** on Polymarket.
2. Runs ensemble forecasts (ECMWF / ICON / GFS) for the relevant airport station.
3. Applies an **airport-specific heat-bias factor** derived from rolling METAR observations.
4. Computes `P(YES)` for each bucket using a normal-CDF over the ensemble distribution.
5. Opens **paper trades** when `model_prob ≥ 0.85` AND `model_prob - market_yes ≥ 0.05pp` (or the equivalent for NO).
6. Tracks every trade end-to-end and records it in Postgres.
7. Sends Telegram alerts on entry, resolution, and once per day a daily summary.
8. Surfaces everything on a web dashboard.

## High-level component map

```
┌──────────────────────────────────────────────────────────────────────────┐
│                      Railway (single Python process)                     │
│                                                                          │
│  ┌──────────────┐    ┌──────────────────┐   ┌─────────────────────────┐  │
│  │ FastAPI app  │───▶│ Background       │──▶│ Background scheduler    │  │
│  │ (REST + UI)  │    │ tasks (asyncio)  │   │ APScheduler (in-proc)   │  │
│  └──────┬───────┘    └──────┬───────────┘   └──────┬──────────────────┘  │
│         │                   │                      │                     │
│         │                   ▼                      ▼                     │
│         │      ┌─────────────────────┐    ┌──────────────────┐           │
│         │      │ Collectors:         │    │ Engine:          │           │
│         │      │ • Polymarket Gamma  │    │ • Bucket parser  │           │
│         │      │ • Open-Meteo Ens.   │    │ • Distribution   │           │
│         │      │ • METAR (NOAA)      │    │ • Bias factor    │           │
│         │      └────────┬────────────┘    │ • Pricer         │           │
│         │               │                 └────────┬─────────┘           │
│         │               ▼                          │                     │
│         │      ┌────────────────────────────────┐  │                     │
│         │      │ SQLAlchemy → Postgres          │◀─┘                     │
│         │      └────────────────────────────────┘                        │
│         │                                                                │
│         └─── Telegram bot (python-telegram-bot, polling) ───┐            │
│                                                             │            │
└─────────────────────────────────────────────────────────────┼────────────┘
                                                              │
                                                              ▼
                                                       Telegram channel
```

## Stack

| Layer       | Choice                       | Why                                                                             |
| ----------- | ---------------------------- | ------------------------------------------------------------------------------- |
| Runtime     | Python 3.12                  | Familiar (same as tennis bot); good async story                                 |
| HTTP server | FastAPI + Uvicorn            | Async, OpenAPI for free, serves both API & static dashboard                     |
| DB          | Postgres (Railway plugin)    | Reliable, transactional, JSONB for ensemble members                             |
| ORM         | SQLAlchemy 2.x (async) + Alembic | Migrations + clean models                                                    |
| Scheduler   | APScheduler `AsyncIOScheduler` | In-process — same container as the bot, no extra worker dyno                  |
| Telegram    | `python-telegram-bot` v21+   | Webhook OR long-poll; polling is simpler on Railway                             |
| Frontend    | Static HTML/CSS/JS (no React) | Same style as `weather-web-checker`; serve as static from FastAPI              |

**One container, one process.** Everything (web, API, scheduler, Telegram poller) runs inside the same Python event loop. This keeps Railway cost low and DB connections sane.

## Source repos / artifacts to reuse

- **`shaytheboss/polymarketweatherassistwebpage`** — reference for the lookups already built:
  - `POLYMARKET_STATIONS` (20 US ICAO stations with lat/lon + aliases)
  - `ENSEMBLE_MODELS` chain (ecmwf_ifs04 → icon_global → gfs025)
  - `fetchEnsembleForecast()` URL params
  - `buildEnsembleDistribution()` — P10/P25/P50/P75/P90 + 1°C histogram
  - `extractIcaoFromWunderground()` regex
  - Bucket parser regex set (between/above/below/reach/exceed)
  - Dark theme CSS

The new repo ports these from JS → Python, then extends with bias, METAR, trades, alerts.

## Key flows

### Flow A — Market discovery (every 15 min)
```
Gamma /events?q=temperature&active=true ─► filter by US station keyword ─►
  for each market:
    parse bucket (above/below/between, °F/°C)
    extract station: URL ICAO regex OR city alias
    upsert markets table
```

### Flow B — Forecast pricing (every hour)
```
For each open market with resolution_date today..today+7:
  fetch ECMWF IFS04 ensemble (51 members) ─► primary decision distribution
  fetch ICON Global + GFS025 ─► store for display/context only
  load station_bias.bias_factor_c ─►
  shift ECMWF members by bias ─►
  bucket probability via normal CDF over ECMWF-only distribution ─►
  write to forecasts table (one row per model + one consensus=ECMWF row)
```

**Decision model is ECMWF-only.** ICON and GFS are fetched and stored so they can be shown in Telegram alerts and the dashboard for human context, but they have zero weight in `P(YES)` and no influence on trade entry.

### Flow C — Trade evaluation (every 15 min, after pricing)
```
For each open market:
  load latest forecast
  load polymarket YES price
  for side in (YES, NO):
    p_model = forecast.bucket_prob if YES else 1 - bucket_prob
    p_market = yes_price if YES else 1 - yes_price
    if p_model >= 0.85 and (p_model - p_market) >= 0.05:
      if no open trade exists for (market, side):
        open paper trade @ size_usd
        Telegram alert
```

### Flow D — Resolution (every 30 min)
```
For each open trade:
  fetch polymarket market state
  if closed AND outcome_yes is known:
    mark trade WIN/LOSS, compute P&L
    Telegram alert
  else:
    cross-check via METAR daily max (informational only — wait for PM truth)
```

### Flow E — Bias refresh (daily at 02:00 UTC)
```
For each station:
  pull last 14 days of: forecast_p50 (made at T-1) vs METAR-daily-max (observed at T)
  bias_factor_c = mean(metar_max - forecast_p50)
  upsert station_bias
```

### Flow F — Daily summary (daily at 23:00 local per user)
```
trades_today = SELECT WHERE entered_at::date = today
won/lost/open counts + sum(pnl_usd) ─► Telegram broadcast
```

## Repo layout

```
weather-trade-bot/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app — mounts API + dashboard + starts scheduler + bot
│   ├── config.py               # pydantic-settings env loader
│   ├── database.py             # async engine + session factory
│   ├── api/
│   │   ├── __init__.py
│   │   ├── trades.py           # GET /api/trades (list + filter)
│   │   ├── markets.py          # GET /api/markets, /api/markets/{cid}
│   │   ├── stations.py         # GET /api/stations
│   │   ├── stats.py            # GET /api/stats (daily P&L curve)
│   │   └── settings.py         # GET/PUT /api/settings (thresholds, paper size)
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── telegram_bot.py     # build the Application + dispatcher
│   │   ├── handlers.py         # /start /status /open /today /pnl /settings ...
│   │   └── formatters.py       # MarkdownV2 builders (entry/resolution/daily)
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── polymarket.py       # Gamma + CLOB
│   │   ├── open_meteo.py       # Ensemble fetcher
│   │   ├── metar.py            # NOAA METAR
│   │   └── stations.py         # ICAO table (port of JS POLYMARKET_STATIONS)
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── ensemble.py         # buildDistribution (port of JS)
│   │   ├── bucket_parser.py    # regex → {comparator, threshold_f, lo_f, hi_f}
│   │   ├── bias.py             # rolling mean (forecast_p50 - metar_max)
│   │   └── pricer.py           # P(in bucket) via normal CDF
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── scheduler.py        # APScheduler setup; register jobs
│   │   └── jobs.py             # job_discover, job_price, job_eval, job_resolve, job_bias, job_summary
│   ├── models/
│   │   ├── __init__.py
│   │   ├── station.py
│   │   ├── forecast.py
│   │   ├── metar_obs.py
│   │   ├── market.py
│   │   ├── trade.py
│   │   ├── user.py
│   │   └── settings.py
│   └── dashboard/
│       ├── index.html          # trades table + P&L chart
│       ├── settings.html       # thresholds form
│       ├── style.css           # ported from weather-web-checker
│       └── app.js              # vanilla JS, fetches /api/*
├── migrations/                 # Alembic — gen on first run
│   ├── env.py
│   └── versions/
├── tests/                      # pytest — at least unit tests for parser & pricer
├── requirements.txt
├── railway.json                # Railway build/start config
├── Procfile                    # web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
├── alembic.ini
├── .env.example
└── README.md
```

## Non-goals (for V1)

- Live trading via Polymarket SDK (paper only).
- Multi-tour: only US weather temperature markets — not rain, snow, hurricane.
- Cross-day spread bets — only outright bucket markets resolving on a fixed day.
- LLM-based question parsing — start with regex; LLM only as a future fallback.

## Risk register (things to design defensively from day 1)

| Risk                                                | Mitigation                                                          |
| --------------------------------------------------- | ------------------------------------------------------------------- |
| Polymarket question wording is irregular            | Strong regex + per-market manual override field (`station_icao_override`) |
| Open-Meteo returns slightly different member keys per release | Defensive parsing: iterate `daily.*member*` keys      |
| METAR temporarily down                              | Job catches exception, logs warning; bias just doesn't update     |
| Same trade gets opened twice                        | DB unique index on `(market_condition_id, side, status='open')`   |
| Polymarket relay needed (Railway IPs blocked)       | `POLYMARKET_RELAY_URL` env var; reuses tennis bot worker pattern  |
| Timezone bugs (Denver high is at local time)        | Each station has `iana_tz`; all DB timestamps in UTC; convert only on display |
