# 02 — Data Model (Postgres)

All timestamps are `TIMESTAMP WITH TIME ZONE` and stored in UTC. Temperatures are stored in **°C** with `_c` suffixes (Open-Meteo native); the dashboard and Telegram messages render °F when the market is °F-denominated.

## Table: `stations`

Static reference table. Seeded once from `POLYMARKET_STATIONS`.

```sql
CREATE TABLE stations (
  icao          VARCHAR(4)  PRIMARY KEY,
  city          VARCHAR(80) NOT NULL,
  state         VARCHAR(2)  NOT NULL,
  lat           DOUBLE PRECISION NOT NULL,
  lon           DOUBLE PRECISION NOT NULL,
  iana_tz       VARCHAR(40) NOT NULL,    -- e.g. "America/Denver"
  aliases       JSONB       NOT NULL DEFAULT '[]',
  enabled       BOOLEAN     NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_stations_city ON stations (LOWER(city));
```

## Table: `forecasts`

One row per (station, target date, model, fetch). Snapshot of an ensemble run.

```sql
CREATE TABLE forecasts (
  id              BIGSERIAL PRIMARY KEY,
  station_icao    VARCHAR(4)  NOT NULL REFERENCES stations(icao),
  target_date     DATE        NOT NULL,         -- the day being forecast
  model           VARCHAR(40) NOT NULL,         -- ecmwf_ifs04 / icon_global / gfs025
  fetched_at      TIMESTAMPTZ NOT NULL,
  members_c       JSONB       NOT NULL,         -- [21.4, 21.7, 22.1, ...] (51 values for ECMWF)
  p10_c           DOUBLE PRECISION,
  p25_c           DOUBLE PRECISION,
  p50_c           DOUBLE PRECISION,
  p75_c           DOUBLE PRECISION,
  p90_c           DOUBLE PRECISION,
  mean_c          DOUBLE PRECISION,
  stddev_c        DOUBLE PRECISION,
  histogram       JSONB,                        -- [{"bin_c": 21, "n": 4, "prob": 0.078}, ...]
  is_consensus    BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE for the consensus row written after all models
  bias_applied_c  DOUBLE PRECISION NOT NULL DEFAULT 0,
  UNIQUE (station_icao, target_date, model, fetched_at)
);
CREATE INDEX idx_forecasts_lookup ON forecasts (station_icao, target_date, fetched_at DESC);
```

**Consensus row**: after all models fetch, write one extra row with `model='consensus'`. **Decision probability is derived exclusively from ECMWF IFS04 members** — the consensus row is simply the bias-shifted ECMWF distribution. ICON Global and GFS025 are fetched and stored (for the dashboard histogram and Telegram message context), but carry **zero weight in trade decisions**.

| Model        | Decision weight | Display |
| ------------ | --------------- | ------- |
| ecmwf_ifs04  | 1.00            | yes     |
| icon_global  | 0.00            | yes (informational only) |
| gfs025       | 0.00            | yes (informational only) |

The consensus distribution = ECMWF members shifted by `bias_factor_c`.

## Table: `metar_obs`

Hourly observations. Used both for validation and bias factor.

```sql
CREATE TABLE metar_obs (
  id            BIGSERIAL PRIMARY KEY,
  station_icao  VARCHAR(4)  NOT NULL REFERENCES stations(icao),
  observed_at   TIMESTAMPTZ NOT NULL,
  temp_c        DOUBLE PRECISION,
  raw_text      TEXT,
  UNIQUE (station_icao, observed_at)
);
CREATE INDEX idx_metar_lookup ON metar_obs (station_icao, observed_at DESC);
```

**Derived view** — daily max per station (used by bias job + resolution sanity check):

```sql
CREATE VIEW metar_daily_max AS
SELECT
  station_icao,
  (observed_at AT TIME ZONE s.iana_tz)::date AS local_date,
  MAX(temp_c) AS max_temp_c
FROM metar_obs m
JOIN stations s USING (station_icao)
GROUP BY station_icao, local_date;
```

## Table: `station_bias`

Rolling additive bias factor per station. Updated nightly.

```sql
CREATE TABLE station_bias (
  station_icao    VARCHAR(4)  PRIMARY KEY REFERENCES stations(icao),
  bias_factor_c   DOUBLE PRECISION NOT NULL DEFAULT 0,
  sample_count    INTEGER NOT NULL DEFAULT 0,
  window_days     INTEGER NOT NULL DEFAULT 14,
  last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notes           TEXT
);
```

`bias_factor_c = mean(metar_daily_max - forecast_p50_c)` over the last 14 calendar days.

## Table: `markets`

Discovered Polymarket weather markets. Updated by the discovery job.

```sql
CREATE TABLE markets (
  condition_id        VARCHAR(80) PRIMARY KEY,
  event_slug          VARCHAR(200) NOT NULL,    -- for URL construction
  market_slug         VARCHAR(200),
  station_icao        VARCHAR(4)  REFERENCES stations(icao),
  station_override    VARCHAR(4),               -- manual fix when auto-parse is wrong
  question            TEXT NOT NULL,
  resolution_date     DATE NOT NULL,            -- the day being forecast (station local)
  comparator          VARCHAR(8) NOT NULL,      -- 'gte' / 'lte' / 'btw'
  threshold_lo_f      DOUBLE PRECISION,         -- for 'btw' and 'gte'/'lte' the lo is the threshold
  threshold_hi_f      DOUBLE PRECISION,         -- only set for 'btw'
  unit                VARCHAR(2) NOT NULL,      -- 'F' or 'C'
  yes_token_id        VARCHAR(80),
  no_token_id         VARCHAR(80),
  last_yes_price      DOUBLE PRECISION,
  last_no_price       DOUBLE PRECISION,
  last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status              VARCHAR(20) NOT NULL DEFAULT 'open', -- open / resolved / expired
  outcome_yes         BOOLEAN,
  resolved_at         TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_markets_status_date ON markets (status, resolution_date);
CREATE INDEX idx_markets_station ON markets (station_icao);
```

`station_icao` is the auto-detected one (URL ICAO regex first, then city alias). `station_override` lets you manually pin a market that was misparsed without re-running the parser — the pricer uses `COALESCE(station_override, station_icao)`.

## Table: `trades`

Each paper trade. One row per `(market, side)` that was entered.

```sql
CREATE TABLE trades (
  id                      BIGSERIAL PRIMARY KEY,
  market_condition_id     VARCHAR(80) NOT NULL REFERENCES markets(condition_id),
  station_icao            VARCHAR(4)  NOT NULL REFERENCES stations(icao),
  side                    VARCHAR(3)  NOT NULL,    -- 'YES' or 'NO'
  entry_price             DOUBLE PRECISION NOT NULL,
  size_usd                DOUBLE PRECISION NOT NULL DEFAULT 100,
  model_prob              DOUBLE PRECISION NOT NULL,
  market_prob             DOUBLE PRECISION NOT NULL,
  edge_pp                 DOUBLE PRECISION NOT NULL,
  forecast_p50_c          DOUBLE PRECISION,
  forecast_stddev_c       DOUBLE PRECISION,
  bias_applied_c          DOUBLE PRECISION NOT NULL DEFAULT 0,
  status                  VARCHAR(20) NOT NULL DEFAULT 'open',   -- open / won / lost / cancelled
  outcome_yes             BOOLEAN,
  pnl_usd                 DOUBLE PRECISION,
  entered_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at             TIMESTAMPTZ,
  forecast_snapshot       JSONB,         -- frozen ensemble stats at entry time for audit
  CONSTRAINT trades_side_chk CHECK (side IN ('YES','NO')),
  CONSTRAINT trades_status_chk CHECK (status IN ('open','won','lost','cancelled'))
);
-- Prevents accidental double-entries:
CREATE UNIQUE INDEX idx_trades_open_unique
  ON trades (market_condition_id, side)
  WHERE status = 'open';
CREATE INDEX idx_trades_entered ON trades (entered_at DESC);
CREATE INDEX idx_trades_status ON trades (status);
```

### P&L math

Paper trading uses Polymarket settlement convention: each share resolves to **$1 if your side wins, $0 otherwise**. For `size_usd` at `entry_price`:

```
shares = size_usd / entry_price
pnl_won  = shares * (1 - entry_price)   = size_usd * (1/entry_price - 1)
pnl_lost = -size_usd
```

Stored on resolution. Examples:

| Entry | Outcome | PnL on $100         |
| ----- | ------- | ------------------- |
| 0.86  | WIN     | +$16.28             |
| 0.86  | LOSS    | -$100               |
| 0.20  | WIN     | +$400 (long shots)  |
| 0.20  | LOSS    | -$100               |

## Table: `telegram_users`

```sql
CREATE TABLE telegram_users (
  chat_id     BIGINT PRIMARY KEY,
  username    VARCHAR(80),
  is_admin    BOOLEAN NOT NULL DEFAULT FALSE,
  receive_alerts BOOLEAN NOT NULL DEFAULT TRUE,
  daily_summary_hour_local INTEGER NOT NULL DEFAULT 23,   -- hour-of-day in user's tz
  user_tz     VARCHAR(40) NOT NULL DEFAULT 'America/New_York',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## Table: `bot_settings`

Single source of truth for tunables — read at job run-time, never cached longer than one cycle.

```sql
CREATE TABLE bot_settings (
  key          VARCHAR(80) PRIMARY KEY,
  value        TEXT NOT NULL,
  description  TEXT,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by   BIGINT REFERENCES telegram_users(chat_id)
);
```

Seeded with:

| key                       | default | meaning                                          |
| ------------------------- | ------- | ------------------------------------------------ |
| `min_model_prob`          | `0.85`  | Enter only if our probability ≥ this             |
| `min_edge_pp`             | `0.05`  | Enter only if `model - market ≥ this`            |
| `paper_size_usd`          | `100`   | Paper-trade dollar size                          |
| `enable_trading`          | `true`  | Master kill switch                               |
| `bias_window_days`        | `14`    | Rolling window for station bias                  |
| `ecmwf_weight`            | `1.00`  | ECMWF is the sole decision model (100% weight)   |
| `icon_weight`             | `0.00`  | Display-only; zero weight in trade decisions     |
| `gfs_weight`              | `0.00`  | Display-only; zero weight in trade decisions     |
| `forecast_sigma_c_fallback` | `2.5` | σ in °C when only point forecast is available (matches JS) |

## Table: `alerts_log`

Audit trail of every Telegram message — used for dedupe and for "did we send entry alert?" checks.

```sql
CREATE TABLE alerts_log (
  id           BIGSERIAL PRIMARY KEY,
  alert_type   VARCHAR(40) NOT NULL,     -- 'ENTRY' / 'RESOLUTION' / 'DAILY' / 'HEARTBEAT'
  trade_id     BIGINT REFERENCES trades(id),
  chat_id      BIGINT,
  payload      JSONB,
  sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  delivered    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_alerts_trade ON alerts_log (trade_id, alert_type);
```

## Data lifecycle

- `forecasts` — kept indefinitely (cheap; needed for back-testing bias).
- `metar_obs` — kept indefinitely (needed for bias recompute).
- `markets` — kept indefinitely. Status transitions `open → resolved` / `expired`.
- `trades` — kept indefinitely. Status transitions `open → won / lost / cancelled`.
- `alerts_log` — purge after 90 days (cron-style job).

## Seed data location

`app/collectors/stations.py` exports `STATIONS` list, e.g.:

```python
STATIONS = [
  Station(icao="KBKF", city="Denver", state="CO", lat=39.7169, lon=-104.7519,
          iana_tz="America/Denver", aliases=["denver"]),
  Station(icao="KMDW", city="Chicago", state="IL", lat=41.7868, lon=-87.7522,
          iana_tz="America/Chicago", aliases=["chicago"]),
  # ... 18 more
]
```

A one-shot `python -m app.tools.seed_stations` upserts them into `stations` table.
