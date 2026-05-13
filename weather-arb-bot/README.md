# Weather Arbitrage Bot

Automated detection of arbitrage opportunities in weather prediction markets (Polymarket / Kalshi)
using multi-source weather data, weighted ensemble analysis, and real-time Telegram alerts.

## Architecture

```
METAR │ Wunderground │ NWS │ GFS/ECMWF │ PIREP │ Polymarket
                         │
                  PostgreSQL DB
                         │
        ┌────────────────┼────────────────┐
        Analyzer      FastAPI           Telegram Bot
                         │
                    React Dashboard
```

## Quick Start (Local)

```bash
# 1. Clone and copy env
cp .env.example .env
# Edit .env: set DATABASE_URL, TELEGRAM_BOT_TOKEN

# 2. Start services
docker-compose up -d

# 3. Run migrations
docker-compose exec backend alembic upgrade head

# 4. Dashboard (separate terminal)
cd dashboard && npm install && npm run dev
```

## Deploy to Railway

1. Push this repo to GitHub
2. Create a new Railway project → connect the repo
3. Add **PostgreSQL** plugin (Railway auto-sets `DATABASE_URL`)
4. Set environment variables (see `.env.example`)
5. Railway will build the Dockerfile and run `start.sh`
6. Set the Telegram webhook:
   ```
   curl https://api.telegram.org/bot<TOKEN>/setWebhook \
     ?url=https://<RAILWAY_URL>/telegram/webhook/<WEBHOOK_SECRET>
   ```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | ✅ | — | PostgreSQL connection string |
| `TELEGRAM_BOT_TOKEN` | ✅ | — | From @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | ✅ | — | Random secret string |
| `METAR_FETCH_INTERVAL` | | 300 | Seconds between METAR fetches |
| `POLYMARKET_FETCH_INTERVAL` | | 30 | Seconds between price checks |
| `ANALYZER_RUN_INTERVAL` | | 120 | Seconds between analysis runs |
| `MIN_CONFIDENCE_FOR_ALERT` | | 60 | Min confidence score to send alert |
| `MIN_EDGE_FOR_ALERT` | | 0.15 | Min edge (probability points) to trigger |
| `ALERT_DEDUP_MINUTES` | | 30 | Suppress duplicate alerts for N minutes |

## Adding a City

Via the dashboard → **Add City** form, or via API:

```bash
curl -X POST http://localhost:8000/api/cities \
  -H "Content-Type: application/json" \
  -d '{
    "name": "San Francisco",
    "primary_icao": "KSFO",
    "reference_icao": "KHAF",
    "wunderground_url": "https://www.wunderground.com/weather/us/ca/san-francisco",
    "nws_lat": 37.6213,
    "nws_lon": -122.379,
    "timezone": "America/Los_Angeles",
    "buoy_id": "46026"
  }'
```

## Adding a Market (Polymarket)

```bash
curl -X POST http://localhost:8000/api/markets \
  -H "Content-Type: application/json" \
  -d '{
    "city_id": 1,
    "external_id": "polymarket-market-id-here",
    "question": "Highest temp in SF today",
    "event_date": "2026-04-24",
    "resolution_time": "2026-04-25T00:00:00Z"
  }'
```

Then add outcomes (buckets):

```bash
curl -X POST http://localhost:8000/api/markets \
  -H "Content-Type: application/json" \
  -d '{"market_id": 1, "bucket_label": "64-65", "bucket_min": 64, "bucket_max": 65}'
```

## Running Tests

```bash
# Unit tests only (no external API calls)
pytest tests/unit/

# Include integration tests (hits real APIs)
pytest tests/ -m integration
```

## API Reference

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/cities` | List monitored cities |
| POST | `/api/cities` | Add a city |
| GET | `/api/cities/{id}/current` | Current METAR + forecast |
| GET | `/api/cities/{id}/history` | Historical METAR log |
| GET | `/api/cities/{id}/signals` | All current signals |
| GET | `/api/markets` | Active markets |
| GET | `/api/markets/{id}/analysis` | Probability estimates per bucket |
| GET | `/api/markets/{id}/prices` | Historical price series |
| GET | `/api/opportunities` | Active opportunities |
| GET | `/api/opportunities/history` | Closed opportunities |

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/start` | Register and show help |
| `/status` | Current temperature and forecast for all cities |
| `/watch SF` | Start receiving alerts for San Francisco |
| `/unwatch SF` | Stop alerts for San Francisco |
| `/settings` | Configure alert thresholds |
| `/dashboard` | Link to web dashboard |

## Data Sources

| Source | Interval | Notes |
|---|---|---|
| METAR (Aviation Weather) | 5 min | Primary truth for temperature |
| Wunderground | 30 min | Resolution source for most Polymarket markets |
| NWS API | 1 hour | Official US forecast |
| GFS / ECMWF (Open-Meteo) | 1 hour | Numerical model consensus |
| PIREP | 15 min | Pilot reports = upper-air temp leading indicator |
| Polymarket CLOB | 30 sec | Live market prices |
| NDBC Buoys | 1 hour | Sea surface temp for coastal cities |

## Important Notes

- **Resolution source matters.** Read the fine print of each Polymarket market — most SF temperature
  markets resolve using Wunderground. The METAR is a predictor, not the resolution source.
- **All times in DB are UTC (TIMESTAMPTZ).** Convert to local only for display.
- **Do not store state on disk.** Railway containers are ephemeral; everything goes to PostgreSQL.
- **Rate limits:** Aviationweather.gov tolerates ~1 req/5min per station. Wunderground scraping
  should use delays. Polymarket CLOB has documented rate limits.
- **This is a tool, not an oracle.** Run in monitor-only mode for at least a week before trading.
