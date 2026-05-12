# 08 — Environment Variables

All env vars live in Railway → Variables. Local dev uses `.env` (gitignored). See `.env.example` for the full template.

## Required

| Var                       | Example                                                          | Purpose                                                                          |
| ------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `DATABASE_URL`            | `postgresql+asyncpg://user:pass@host:5432/dbname`                | Postgres connection. Railway exposes this automatically with the Postgres plugin. SQLAlchemy needs `+asyncpg` (Railway gives `postgresql://`; rewrite in `config.py`). |
| `TELEGRAM_BOT_TOKEN`      | `7531234567:AAH...`                                              | From [@BotFather](https://t.me/BotFather)                                        |
| `ADMIN_CHAT_IDS`          | `12345678,87654321`                                              | Comma-separated chat IDs that can run admin commands.                            |
| `SETTINGS_ADMIN_TOKEN`    | `random32charsecret`                                             | Bearer token for `PUT /api/settings`.                                            |
| `PORT`                    | (Railway sets it)                                                | Uvicorn binds to it.                                                             |

## Polymarket

| Var                         | Default                                            | Purpose                                                                |
| --------------------------- | -------------------------------------------------- | ---------------------------------------------------------------------- |
| `POLYMARKET_GAMMA_URL`      | `https://gamma-api.polymarket.com`                 | Override only if relay needed.                                         |
| `POLYMARKET_CLOB_URL`       | `https://clob.polymarket.com`                      | Same.                                                                  |
| `POLYMARKET_RELAY_URL`      | empty                                              | Cloudflare Worker URL to bypass Railway IP blocks. When set, all requests go through it. |
| `POLYMARKET_PROXY_URL`      | empty                                              | Optional HTTPS proxy (alt to relay). Used by tennis bot.               |

## Weather data

| Var                       | Default                                                          | Purpose                                                              |
| ------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------- |
| `OPEN_METEO_BASE_URL`     | `https://ensemble-api.open-meteo.com`                            | Open-Meteo ensemble API.                                             |
| `METAR_BASE_URL`          | `https://aviationweather.gov/api/data/metar`                     | NOAA METAR JSON endpoint.                                            |

## Trading defaults (override DB-stored values on first run)

| Var                       | Default | Maps to setting              |
| ------------------------- | ------- | ---------------------------- |
| `DEFAULT_MIN_MODEL_PROB`  | `0.85`  | `min_model_prob`             |
| `DEFAULT_MIN_EDGE_PP`     | `0.05`  | `min_edge_pp`                |
| `DEFAULT_PAPER_SIZE_USD`  | `100`   | `paper_size_usd`             |
| `DEFAULT_BIAS_WINDOW_DAYS`| `14`    | `bias_window_days`           |
| `DEFAULT_ENABLE_TRADING`  | `true`  | `enable_trading`             |

These are only used the first time the app boots into an empty `bot_settings` table. Subsequent changes happen via `/setprob` etc. and persist in DB.

## Scheduler intervals (seconds)

| Var                          | Default | Job                              |
| ---------------------------- | ------- | -------------------------------- |
| `JOB_DISCOVER_MARKETS_SEC`   | `900`   | `job_discover_markets` (15 min)   |
| `JOB_REFRESH_FORECASTS_SEC`  | `3600`  | `job_refresh_forecasts` (60 min)  |
| `JOB_FETCH_METAR_SEC`        | `900`   | `job_fetch_metar` (15 min)        |
| `JOB_EVALUATE_TRADES_SEC`    | `900`   | `job_evaluate_trades` (15 min)    |
| `JOB_CHECK_RESOLUTIONS_SEC`  | `1800`  | `job_check_resolutions` (30 min)  |
| `JOB_HEARTBEAT_SEC`          | `14400` | `job_heartbeat` (4 h)             |
| `JOB_DAILY_SUMMARY_SEC`      | `900`   | `job_daily_summary` (15 min, fires at user's local hour) |
| `JOB_REFRESH_BIAS_CRON`      | `0 2 * * *` | Cron expr — daily at 02:00 UTC |

## Operational

| Var                   | Default                | Purpose                                                                       |
| --------------------- | ---------------------- | ----------------------------------------------------------------------------- |
| `LOG_LEVEL`           | `INFO`                 | Python `logging` level.                                                       |
| `SENTRY_DSN`          | empty                  | Optional error tracking.                                                      |
| `TZ`                  | `UTC`                  | Container TZ. Always UTC — never let local TZ pollute timestamps.             |
| `APP_VERSION`         | (set by CI)            | Returned by `/health`.                                                        |

## `.env.example` (verbatim)

```dotenv
# === Required ===
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/weatherbot
TELEGRAM_BOT_TOKEN=
ADMIN_CHAT_IDS=
SETTINGS_ADMIN_TOKEN=change-me-32-chars-or-more

# === Polymarket ===
POLYMARKET_GAMMA_URL=https://gamma-api.polymarket.com
POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLYMARKET_RELAY_URL=
POLYMARKET_PROXY_URL=

# === Weather data ===
OPEN_METEO_BASE_URL=https://ensemble-api.open-meteo.com
METAR_BASE_URL=https://aviationweather.gov/api/data/metar

# === Trading defaults (used only on first boot, then DB takes over) ===
DEFAULT_MIN_MODEL_PROB=0.85
DEFAULT_MIN_EDGE_PP=0.05
DEFAULT_PAPER_SIZE_USD=100
DEFAULT_BIAS_WINDOW_DAYS=14
DEFAULT_ENABLE_TRADING=true

# === Scheduler ===
JOB_DISCOVER_MARKETS_SEC=900
JOB_REFRESH_FORECASTS_SEC=3600
JOB_FETCH_METAR_SEC=900
JOB_EVALUATE_TRADES_SEC=900
JOB_CHECK_RESOLUTIONS_SEC=1800
JOB_HEARTBEAT_SEC=14400
JOB_DAILY_SUMMARY_SEC=900
JOB_REFRESH_BIAS_CRON=0 2 * * *

# === Operational ===
LOG_LEVEL=INFO
TZ=UTC
SENTRY_DSN=
```
