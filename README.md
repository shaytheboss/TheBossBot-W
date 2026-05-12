# TheBossBot-W — Weather Polymarket Trading Bot

Automated paper-trading bot for US weather temperature markets on Polymarket.

## Pitch

We discovered (via the [weather-web-checker](https://github.com/shaytheboss/polymarketweatherassistwebpage) project) that ensemble forecasts — especially **ECMWF IFS04 (51 members)** — combined with an **airport-specific heat-bias correction** (derived from rolling METAR observations) systematically beat Polymarket's implied probabilities on US temperature markets by 5–15pp on close calls.

This bot:

1. Watches **20 US airport weather stations** for active Polymarket temperature markets.
2. Runs the ensemble forecast for each market's resolution date.
3. Applies the **airport-specific heat-bias factor** (rolling 14-day mean of `METAR_max − forecast_P50`).
4. Computes `P(YES)` per bucket using the bias-shifted ensemble distribution.
5. Opens a **paper trade** when `P_model ≥ 85%` AND `P_model − P_market ≥ 5pp`.
6. Tracks every trade end-to-end in Postgres.
7. Sends Telegram alerts: trade entry, market resolution, daily summary.
8. Exposes a dashboard for trade history, P&L, and tunable thresholds.

**Paper trading only in V1** — no real Polymarket execution.

## Documents in this folder

| File                          | What's inside                                                                      |
| ----------------------------- | ---------------------------------------------------------------------------------- |
| `01-ARCHITECTURE.md`          | System overview, components, stack, repo layout                                    |
| `02-DATA-MODEL.md`            | Postgres schema (stations, forecasts, metar, markets, trades, settings, alerts)    |
| `03-TELEGRAM-BOT.md`          | Commands (user + admin) and message templates                                      |
| `04-DEVELOPMENT-PLAN.md`      | Phased build order with acceptance criteria per phase                              |
| `05-BIAS-METHODOLOGY.md`      | Airport heat-bias math + METAR validation flow                                     |
| `06-POLYMARKET-PARSING.md`    | Market discovery + question parser regex set                                       |
| `07-DASHBOARD-SPEC.md`        | Web UI pages + API endpoints                                                       |
| `08-ENV-VARS.md`              | Every env var, defaults, `.env.example`                                            |
| `09-RAILWAY-SETUP.md`         | Step-by-step deployment playbook                                                   |

## TL;DR for development

```
Phase 0  Scaffold        → /health responds
Phase 1  DB + stations   → 20 stations seeded
Phase 2  Telegram bot    → /start /help /status work
Phase 3  Ensemble        → forecasts populated hourly
Phase 4  METAR           → observations populated
Phase 5  Bias factor     → station_bias populated nightly
Phase 6  Market discovery → markets table populated
Phase 7  Pricer          → can compute P(bucket) for any market
Phase 8  Trade engine    → opens paper trades + sends Telegram alerts
Phase 9  Resolution      → trades close + alerts sent on Polymarket resolve
Phase 10 Daily summary   → end-of-day report per registered user
Phase 11 Dashboard       → web UI for trades + settings
Phase 12 Polish          → /diag, auto-pause on failure, observability
```

## Key thresholds (defaults; tunable via DB + Telegram)

| Setting           | Default | Meaning                                                |
| ----------------- | ------- | ------------------------------------------------------ |
| `min_model_prob`  | 0.85    | Our model probability must be ≥ this to consider trade |
| `min_edge_pp`     | 0.05    | `P_model − P_market` must be ≥ this                    |
| `paper_size_usd`  | 100     | Dollar size per paper trade                            |
| `bias_window_days`| 14      | Rolling window for station bias                        |
| `ecmwf_weight`    | 0.55    | ECMWF dominance in the consensus                       |
| `icon_weight`     | 0.25    |                                                        |
| `gfs_weight`      | 0.20    |                                                        |

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2.x async, Postgres
- python-telegram-bot v21 (long-polling)
- APScheduler (in-process)
- Open-Meteo Ensemble API (ECMWF / ICON / GFS)
- NOAA METAR API
- Polymarket Gamma + CLOB APIs

Deployed as a single container on Railway.

## What's intentionally out of scope (V1)

- Real money execution (paper only)
- Non-temperature weather markets (rain, snow, hurricane, wind)
- Multi-day or hourly resolution markets
- Markets outside the 20 watched US stations
- LLM-based question parsing (regex first; LLM is a future fallback)

## Reference projects

- `shaytheboss/polymarketweatherassistwebpage` — the web frontend whose station table, ensemble logic, and bucket parser we port to Python here.
- `shaytheboss/Polymarkettenniscomparebot` — the Telegram bot scaffolding pattern (handlers, telegram_bot.py structure, broadcast util, scheduler integration) we reuse.

## License

Private use.
