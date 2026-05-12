# 09 — Railway Setup & Deployment

Step-by-step playbook for going from "empty repo" to "production bot".

## Prereqs

- GitHub repo: `shaytheboss/TheBossBot-W`
- Railway account
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID (use [@userinfobot](https://t.me/userinfobot))

## Step 1 — Connect repo to Railway

1. Railway → **New Project** → **Deploy from GitHub repo**.
2. Pick `shaytheboss/TheBossBot-W`.
3. Railway auto-detects Python; will use `Procfile` for the start command.

## Step 2 — Add Postgres plugin

1. Project page → **+ New** → **Database** → **Add PostgreSQL**.
2. Railway creates a Postgres service in the same project.
3. **Reference the connection string** in the web service:
   - Web service → Variables → **+ Variable** → **Add Reference** → select `Postgres.DATABASE_URL` → name it `DATABASE_URL_RAW`.
   - Add another raw variable `DATABASE_URL=${{DATABASE_URL_RAW}}` so we can normalize the URL in `config.py` (Railway gives `postgresql://`, asyncpg needs `postgresql+asyncpg://`).

> In `app/config.py`:
> ```python
> def _normalize_db_url(url: str) -> str:
>     if url.startswith("postgres://"):
>         url = url.replace("postgres://", "postgresql://", 1)
>     if "+asyncpg" not in url:
>         url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
>     return url
> ```

## Step 3 — Set required env vars

Web service → Variables → set:

```
TELEGRAM_BOT_TOKEN=...
ADMIN_CHAT_IDS=YOUR_CHAT_ID
SETTINGS_ADMIN_TOKEN=<32-char-random>
TZ=UTC
LOG_LEVEL=INFO
```

(See `08-ENV-VARS.md` for the full list and defaults.)

## Step 4 — Configure start command

`Procfile` at repo root:

```
web: alembic upgrade head && python -m app.tools.seed_stations && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

This:
1. Runs DB migrations.
2. Seeds the 20 stations (idempotent).
3. Starts the FastAPI app, which boots the scheduler and Telegram bot in its lifespan.

`railway.json`:

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": { "builder": "NIXPACKS" },
  "deploy": {
    "startCommand": "alembic upgrade head && python -m app.tools.seed_stations && uvicorn app.main:app --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/health",
    "healthcheckTimeout": 100,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

`requirements.txt` covers Python deps (Nixpacks installs them automatically).

## Step 5 — Domain

Railway → Settings → **Generate Domain**. You'll get something like `the-boss-bot-w-production.up.railway.app`. That's the URL for the dashboard.

## Step 6 — Telegram bot polling vs webhook

V1 = **long polling**. No URL setup needed on Telegram side. The Python process opens a long-poll connection.

If you ever switch to webhook, add an extra Railway variable and call `bot.set_webhook(url=f"{public_url}/telegram/webhook")` on startup.

## Step 7 — First-run smoke test

Once deployed:

1. Visit `https://<your-app>.up.railway.app/health` → `{"ok": true, "version": "..."}`.
2. Open Telegram, send `/start` to the bot → welcome message.
3. Send `/diag` → connectivity report (all green expected).
4. Wait 1 hour → `/status` shows `Forecasts: 20 stations × N days` updated.
5. Wait 15 min for first discovery cycle → `/markets` shows watched markets.

## Step 8 — Optional: Cloudflare relay (if Polymarket blocks Railway)

The tennis bot showed that Railway IPs can be blocked by Polymarket's WAF. The relay pattern:

1. Cloudflare → Workers → Create Worker named `polymarket-relay`.
2. Paste the `cloudflare-worker.js` from the tennis bot repo (`/Polymarkettenniscomparebot/cloudflare-worker.js`) which already handles `/gamma/*` and `/clob/*`.
3. Deploy. Copy the worker URL (`https://polymarket-relay.<your-account>.workers.dev`).
4. Railway → web service → Variables → set `POLYMARKET_RELAY_URL=https://polymarket-relay....workers.dev`.
5. Redeploy.

`app/collectors/polymarket.py:_url(path)` checks `settings.polymarket_relay_url` and routes through it.

## Step 9 — Monitoring

Railway gives you logs and metrics out of the box. Quick wins:

- Pin logs filter `[HEARTBEAT]` to see the running counter.
- Set up Railway notifications for crashes (Settings → Notifications).
- Optional: add Sentry by setting `SENTRY_DSN`; the app initializes it in `app/main.py` if present.

## Step 10 — Cost expectations

- 1 web container (~$5/mo on Railway hobby tier)
- 1 Postgres (~$5/mo)
- Egress: trivial (~MB/day)

Total: under $15/mo for a single-instance bot polling APIs every 15 min.

## Recovery playbook

| Symptom                                         | Fix                                                                       |
| ----------------------------------------------- | ------------------------------------------------------------------------- |
| Bot stops responding                            | Railway → Deployments → Restart                                           |
| Postgres connections exhausted                  | Lower `pool_size` in `database.py`; check for connection leaks            |
| Telegram polling stuck                          | Restart container; check `TELEGRAM_BOT_TOKEN` not rotated                  |
| All Polymarket calls return 403                 | Set `POLYMARKET_RELAY_URL` (see Step 8)                                   |
| Trades not opening despite high edge            | Send `/diag` → check `enable_trading=true`; verify forecasts are fresh    |
| Forecasts stale                                 | Send `/recompute_bias`; check Open-Meteo status; check job-error logs     |

## Backups

Railway Postgres → Settings → **Backups**: turn on daily snapshots. Keep 7 days. Restore via PITR if needed.
