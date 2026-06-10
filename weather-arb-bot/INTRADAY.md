# ⚡ Intraday Trading Subsystem — Design & Strategy

## Why a separate intraday bot?

The daily bot answers: *"given today's forecasts, which bucket will win?"* — its edge
comes from blending NWP models better than the market. Its uncertainty is the
**forecast sigma** (±2.5°F same-day).

The intraday bot answers a different question: *"given what has ALREADY HAPPENED
today, what can still change?"* — its edge comes from the market being slow to
absorb real-time observations. Its uncertainty is the **residual heating sigma**,
which collapses toward zero as the day progresses.

### The three physical facts the strategy is built on

1. **The running max is monotonic.** `metar_today_max_f` only goes up. Any bucket
   entirely below the observed max is *mathematically dead* — its YES cannot win.
   An open-ended "≥X" bucket whose floor was already touched is *mathematically
   won*. These are **locks** — the highest-quality trades available anywhere.

2. **Remaining heating potential decays through the day.** Before ~11:00 local
   there are hours of heating ahead and the intraday view adds little over the
   daily blend. Between 11:00 and the climatological peak window (~14:00–17:00
   local) every hour kills part of the upside. After the peak has demonstrably
   passed (temp falling for 90+ minutes, ≥1.5°F below the max, after 14:00) the
   residual sigma is ~0.3°F — near-certainty.

3. **The market lags the METAR.** Bucket prices in less-liquid city markets keep
   showing 5–15¢ of "maybe" long after the maximum is effectively locked in.
   That spread between near-certainty and market price is the intraday edge.

## The probability model

Let `M` = running max so far, `X` = the max that *additional* heating would reach,
modeled as `X ~ N(μ, σ_h)` where:

- `μ` (expected final max) = `M + max(0, F_blend − M) × g(h)` where `F_blend` is the
  blended forecast high (reusing the daily signal aggregator's sources) and
  `g(h)` is the **gain weight**: fraction of the day's heating still ahead,
  decaying linearly from 1.0 at `start_hour` (10:00) to 0.0 at `peak_end` (17:00).
- `σ_h` (**hour-dependent sigma**) shrinks with hours-to-peak-end:

  | hours to peak end | σ (°F) |
  |---|---|
  | ≥ 6 | 2.2 |
  | 4–6 | 1.6 |
  | 2–4 | 1.0 |
  | 1–2 | 0.6 |
  | < 1 | 0.4 |
  | peak passed | 0.3 |

The final max is `max(M, X)` — a truncated distribution with a point mass at `M`.
This yields clean closed-form bucket probabilities:

- bucket entirely below `M` → **P ≈ 0** (lock: `yes_impossible`)
- open-ended "≥lo" with `M ≥ lo` → **P ≈ 1** (lock: `yes_locked`)
- bucket containing `M` (lo ≤ M < hi) → `P = Φ((hi−μ)/σ)`
- bucket above `M` (lo > M) → `P = Φ((hi−μ)/σ) − Φ((lo−μ)/σ)`

All parameters (`start_hour`, `peak_start/end`, sigma table, cooling detection
thresholds) live in `IntradayParams` / settings — **explicitly designed for
tuning** as data accumulates.

## Which models/data feed it

| Source | Role | Cadence (existing) |
|---|---|---|
| **METAR** | running max, current temp, trend — the core signal | 5 min ✓ |
| **HRRR** | best 0–18h US model — highest weight pre-peak | 1 h ✓ |
| **NWS** | hourly-updated official forecast | 1 h ✓ |
| GFS/ECMWF + ensembles | blend anchor + spread sanity | 1 h ✓ |
| Wunderground / ICON / Tomorrow.io / Meteosource | blend members | 30m–4h ✓ |
| Polymarket book | entry price, spread filter | 5 min ✓ |

**Phase 2 candidates** (documented, not built): Open-Meteo *hourly* temperature
curve (shape of the heating ramp), satellite cloud-cover deltas (early cooling
detection), 1-minute ASOS data (faster than METAR cycles).

## Entry economics

Near-certainty means smaller edges are still profitable, so intraday thresholds
differ from daily (all configurable, `intraday_*` in settings):

| Parameter | Default | Rationale |
|---|---|---|
| `intraday_start_hour_local` | 10.0 | before that the daily bot is the right tool |
| `intraday_min_certainty_alert` | 0.90 | alert floor |
| `intraday_min_certainty_buy` | 0.94 | virtual-buy floor |
| `intraday_min_edge` | 0.05 | 5¢ is real money at 95%+ certainty |
| `intraday_max_edge` | 0.40 | bigger gap ⇒ we're probably wrong |
| `intraday_max_book_spread` | 0.10 | liquidity guard |

## Separation guarantees (nothing existing can break)

- **Own DB table** `intraday_opportunities` (migration 011) — zero schema changes
  to `opportunities`.
- **Own package** `app/intraday/` (estimator + detector). Reuses existing pure
  functions (`_bucket_to_f_bounds`, `_norm_cdf`) and the signal aggregator
  **read-only** — never writes to shared state.
- **Own scheduler job** `job_run_intraday` behind `intraday_enabled` flag —
  disabled = the subsystem doesn't exist.
- **Own Telegram format** (⚡ INTRADAY headline) and own dedup state.
- **Settlement** runs inside `job_check_resolutions` *after* the daily flow, in
  its own try/except — an intraday failure can never affect daily settlement.
- **Own admin tab** (⚡ Intraday) and own API endpoints (`/admin/intraday/*`).
- **Per-city opt-in**: `cities.intraday_enabled` (default ON, toggle in Cities tab).

## Learning loop (the whole point)

Every intraday opportunity records: `local_hour`, `hours_to_peak_end`,
`running_max_f`, `expected_final_max_f`, `sigma_used`, `lock_state`, certainty,
entry price. After settlement, the stats screen breaks down win-rate and P&L:

- **by detection hour** → finds the hour from which we're actually reliable
- **by certainty band** → calibration: does stated 95% win 95%?
- **by lock vs non-lock** → are the "mathematical" trades really safe? (rounding,
  station-vs-resolution mismatches will show up here)
- **by city** → which stations report fast/accurately enough for intraday

These four tables are exactly the dials for tuning the sigma table, the gain
curve, and the entry thresholds in the next iteration.
