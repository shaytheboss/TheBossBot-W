# 03 — Telegram Bot Spec

The bot uses `python-telegram-bot` v21+ with **long-polling** (no webhook setup needed on Railway). All messages are MarkdownV2 (with proper escaping), backticks for code, links with `[label](url)`.

## Authentication

- `TELEGRAM_BOT_TOKEN` env var (from BotFather)
- `ADMIN_CHAT_IDS` env var — comma-separated. Only these chats can:
  - Change settings (`/setedge`, `/setprob`, `/setsize`)
  - Pause / resume trading (`/pause`, `/resume`)
  - Override market station (`/setstation`)

Non-admin users can still read (`/status`, `/open`, `/today`, `/pnl`) but only after `/start` registers them.

## Commands

### User commands

| Command | Args | Effect |
| ------- | ---- | ------ |
| `/start` | – | Register the chat in `telegram_users`. Send welcome + help. |
| `/help` | – | Show full help message. |
| `/status` | – | Live counters: open trades, today P&L, lifetime P&L, last forecast fetch. |
| `/open` | – | List currently open trades (up to 15). |
| `/today` | – | Today's trades (entered or resolved today) + day P&L. |
| `/yesterday` | – | Yesterday's summary (counts, P&L, win-rate). |
| `/pnl` | `[N]` | Lifetime P&L; with `N` show last N days only. |
| `/markets` | – | Top 10 watched markets right now (ordered by edge). |
| `/forecast` | `<city>` | One-off forecast lookup. Same output as the website. |
| `/stations` | – | List enabled stations. |
| `/settings` | – | Show current thresholds + sizes. |
| `/mute` | – | Stop alerts for this chat (keeps trades running). |
| `/unmute` | – | Re-enable alerts. |

### Admin commands

| Command | Args | Effect |
| ------- | ---- | ------ |
| `/setprob` | `0.85` | Set `min_model_prob`. |
| `/setedge` | `0.05` | Set `min_edge_pp`. |
| `/setsize` | `100` | Set `paper_size_usd`. |
| `/pause` | – | Set `enable_trading=false`. New trades are not opened; existing trades keep resolving. |
| `/resume` | – | Set `enable_trading=true`. |
| `/setstation` | `<condition_id_or_slug> <ICAO>` | Force a market to a specific station. |
| `/cancel` | `<trade_id>` | Manually mark a paper trade `cancelled` (e.g. data error). |
| `/recompute_bias` | `[ICAO]` | Re-run nightly bias job ad-hoc, optionally for one station. |
| `/diag` | – | One-shot connectivity test (Polymarket, Open-Meteo, METAR). |

## Notification types

### 1. Entry alert

Sent immediately when a new paper trade is opened (`status='open'`).

```
🎯 NEW PAPER TRADE — YES

📍 Denver (KBKF)
❓ Will Denver be above 75°F on May 14, 2026?
📅 Resolves: 2026-05-14 (local)

📐 Our model
  ECMWF (51 members) leads consensus
  Forecast P50: 27.6°C (81.7°F) + bias +1.5°C
  Adjusted P50: 29.1°C (84.4°F)
  P(High > 75°F): 91.2%

📉 Polymarket
  YES ask: 86.0¢
  NO  ask: 14.0¢
  Volume 24h: $12,340

➡️ Edge: +5.2pp on YES  ⚡
💰 Paper size: $100  (shares: 116.3)

🔗 https://polymarket.com/event/will-denver-be-above-75f-on-may-14
🆔 Trade #142  |  ⏰ 14:32 UTC
```

### 2. Resolution alert

When Polymarket resolves the market and we mark the trade `won`/`lost`.

```
✅ WON — Trade #142

📍 Denver  |  YES @ 86¢
❓ Was Denver above 75°F on May 14?

Actual METAR daily max: 81°F (27.2°C)
Our forecast was: 84.4°F  ← off by 3.4°F
Bias used: +1.5°C  (window 14d, samples 12)

P&L: +$16.28  (+16.3%)
🏆 Today: 3W / 1L / +$28.50
🏆 Lifetime: 24W / 11L (68.6%) / +$300.28
```

For losses, use `❌ LOST` header with the same template.

### 3. Daily summary

Sent at the user's `daily_summary_hour_local` (default 23:00 local). One message per registered chat.

```
📊 DAY REPORT — Tuesday May 12

Today's trades: 4
  ✅ Won: 3 (+$45.20)
  ❌ Lost: 1 (-$14.10)
  📈 Net: +$31.10

Still open: 2
  Denver — YES @ 91¢ (resolves May 13)
  Tampa  — NO  @ 78¢ (resolves May 14)

Lifetime
  23W / 11L (67.6%)
  Net: +$284.10
  Best day: +$87.40 (Apr 28)
  Worst day: -$45.00 (May 04)

⏰ Next forecast refresh in 14 min
```

### 4. Heartbeat (every 4h, optional)

Quieter check-in:

```
🟢 Bot alive — 14:00 UTC
Open: 5  |  Today: +$24  |  Last forecast: 13:48
```

Send only if `receive_alerts=TRUE` and `is_admin=TRUE` (admins get heartbeats, users don't, to reduce noise).

### 5. Error / paused alert

When a job throws repeatedly (3 failures in a row) or trading is paused.

```
⚠️ Trading PAUSED
Reason: 3 consecutive Polymarket API failures
Last error: HTTP 403 at 14:12 UTC
Use /resume after fixing.
```

## Message formatter contract

All formatters live in `app/bot/formatters.py` and return a `str` ready to pass to `update.message.reply_text(text, parse_mode='MarkdownV2', disable_web_page_preview=True)`.

```python
def fmt_entry(trade: Trade, market: Market, forecast: Forecast, station: Station) -> str: ...
def fmt_resolution(trade: Trade, market: Market, metar_max_c: float | None) -> str: ...
def fmt_daily_summary(rows: list[dict], user: TelegramUser, lifetime: dict) -> str: ...
def fmt_heartbeat(stats: dict) -> str: ...
def fmt_status(stats: dict) -> str: ...
def fmt_open_trades(trades: list[Trade]) -> str: ...
def fmt_forecast_oneshot(station: Station, forecast: Forecast, markets: list[Market]) -> str: ...
```

`_esc()` helper escapes MarkdownV2 special chars (`_ * [ ] ( ) ~ \` > # + - = | { } . !`).

## Broadcast vs targeted

- **Entry / Resolution / Daily** — broadcast to every chat with `receive_alerts=TRUE`.
- **Heartbeat / Error** — admin chats only.
- **Command responses** — only the requesting chat.

`app/bot/telegram_bot.py:broadcast(text, admin_only=False)` handles fan-out, with `try/except` per chat so one bad chat doesn't block the rest.

## Rate limits

Telegram allows ~30 msgs/sec across all chats. With <100 chats we're fine; throttle via `asyncio.sleep(0.05)` between sends just in case.

## Onboarding flow

1. User opens chat with the bot, sends `/start`.
2. Bot stores chat in `telegram_users` (non-admin by default).
3. Bot replies with welcome + the `/help` text.
4. Admins manually set `is_admin=true` via SQL on first run, or via env var `ADMIN_CHAT_IDS` that's applied on `/start`.

```python
# On /start:
if chat_id in settings.admin_chat_ids:
    user.is_admin = True
```
