# Research scripts

Offline analyses behind the entries in [`../../RESEARCH.md`](../../RESEARCH.md).
They are not part of the bot: nothing in `app/` imports them, they are not linted
or tested, and they cost nothing on Railway. Each file's docstring says what it
answers and how to run it.

Run them with a Python that has `pandas`, `numpy` and `httpx` (the bot's
`requirements.txt` plus `pandas`), and with `-I` when they read downloaded files.

## The data folder (`<DATA>`)

Most scripts take one argument, a working folder laid out like this:

| Path | What | How to get it |
|---|---|---|
| `<DATA>/shadow_in/shadow.csv` | Hourly model-vs-market snapshot of every bucket (`model_p`, `market_p`, `bid`, `ask`, `forecast_high_f`, `v2_p`, `won`, …) | Dashboard → Stats → set the from-date → **⬇ Shadow study (ZIP, small)**, unzip. Kept 45 days. |
| `<DATA>/gamma/closed_*.json` | Closed weather events with their buckets, token ids and settled prices | `00_fetch_gamma_closed_events.py` |
| `<DATA>/gamma/ht_open.json` | Open temperature events (only for `14_`) | Gamma `/events?tag_slug=weather&closed=false` (gzip), keep `highest-temperature-*` |
| `<DATA>/ph/ph_series.npz`, `ph_meta.json` | 1-minute YES price per bucket over the city's event day | `04_fetch_minute_prices.py` |
| `real-track-*.csv` | The Real track table | Dashboard → Real track → **⬇ CSV** |
| `leaderboard.json`, `trades_nov.json` | Weather leaderboard and the leaders' trades | `08_` then `09_`, run in the same folder |

## Public endpoints and their quirks (checked 2026-11-27)

- **Gamma** `gamma-api.polymarket.com/events` — pages with `limit` > 50 are served
  only gzip-compressed (send `Accept-Encoding: gzip`, or curl `--compressed`).
- **CLOB** `clob.polymarket.com/prices-history?market=<token>&startTs&endTs&fidelity=1`
  — needs a `User-Agent` header. The price `p` is the **mid** (checked by `05_`).
- **Data API v1 and `lb-api` are retired (HTTP 410).** Use
  `data-api.polymarket.com/v2/…`: `trades?user=<wallet>&limit=1000` and
  `leaderboard?category=weather&time_period=day|week|month|all&limit=100`.
  Paging is cursor-only (`&cursor=<next_cursor>`); an `offset` parameter is an
  error. Trades carry no maker/taker flag: compare the price with the minute mid.
- **aviationweather.gov** METAR — a request for many stations and many hours is
  silently capped (~800 records); ask for ~8 stations per request.
- Not reachable from the cloud sessions: Open-Meteo's historical-forecast and
  previous-runs APIs, `docs.polymarket.com`, `deepmind.google`.

## Index

| Script | Question | RESEARCH.md |
|---|---|---|
| `01_realtrack_csv_breakdown.py` | Where does the live Real track win or lose? | R-2026-11-27a |
| `02_shadow_alpha_filter.py` | Does alpha's ≥91% add anything beyond a 70–85¢ price? | R-2026-11-27b |
| `03_shadow_far_buckets_and_hourly_jumps.py` | Far-from-forecast buckets; hourly jumps | R-2026-11-27d, e |
| `04`–`07` | Minute-level jumps: fetch, price check, fade/follow, robustness | R-2026-11-27f |
| `08`–`12` | What the profitable wallets do, and whether copying them pays | R-2026-11-27g |
| `13`, `14` | Replay the nearby-station signal on today's METARs and prices | R-2026-11-26a |
