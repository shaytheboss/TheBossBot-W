# 05 — Airport-Heat Bias Methodology

The Polymarket market resolves on the **airport METAR daily high**, which is systematically warmer than:
- the city-center weather station, and
- the gridded Open-Meteo ensemble at the airport's exact lat/lon (the ensemble grid is ~25 km; the model can't see the runway tarmac).

This bias is real and roughly constant per airport over weeks/seasons. We measure it and add it as a correction to the ensemble forecast.

## Definition

For station `s` and target date `d`:

```
bias_c(s, d) = actual_metar_daily_max_c(s, d) - forecast_p50_c(s, made_at=d-1)
```

`forecast_p50_c` is taken from the consensus row (`model='consensus'`), fetched at T-1 day. This isolates "airport warmth" from "lead-time error" — both are real, but only the first is systematic per-airport.

## Aggregation — rolling 14 days

```
bias_factor_c(s) = mean(bias_c(s, d_i)) for d_i in last 14 calendar days where both METAR & forecast exist
```

Implemented in `app/engine/bias.py`:

```python
async def compute_bias_for_station(
    db: AsyncSession,
    station_icao: str,
    window_days: int = 14,
) -> BiasResult:
    end = today_local(station_icao)        # in station's tz
    start = end - timedelta(days=window_days)

    rows = await db.execute(
        text("""
        SELECT d.local_date,
               m.max_temp_c            AS actual_c,
               f.p50_c + f.bias_applied_c AS forecast_p50_c
        FROM metar_daily_max d
        JOIN forecasts f
          ON f.station_icao = d.station_icao
         AND f.target_date  = d.local_date
         AND f.model        = 'consensus'
         AND f.fetched_at::date = (d.local_date - INTERVAL '1 day')::date
        WHERE d.station_icao = :icao
          AND d.local_date BETWEEN :start AND :end
        """),
        {"icao": station_icao, "start": start, "end": end},
    )
    samples = [(r.actual_c - r.forecast_p50_c) for r in rows]
    if not samples:
        return BiasResult(bias_c=0.0, samples=0, notes="no data")
    return BiasResult(
        bias_c=mean(samples),
        samples=len(samples),
        notes=f"window={window_days}d, samples={len(samples)}, stddev={pstdev(samples):.2f}",
    )
```

Result is persisted in `station_bias`.

## Application in the pricer

```python
# pricer.py
def price_market(market, forecast, bias_c):
    shifted_members_c = [m + bias_c for m in forecast.members_c]
    shifted_mean = forecast.mean_c + bias_c
    shifted_p50 = forecast.p50_c + bias_c

    # Empirical (preferred): fraction of members satisfying the bucket
    if market.comparator == 'gte':
        threshold_c = f_to_c(market.threshold_lo_f) if market.unit == 'F' else market.threshold_lo_f
        prob_emp = sum(1 for m in shifted_members_c if m >= threshold_c) / len(shifted_members_c)
        # CDF cross-check
        prob_cdf = 1 - norm_cdf((threshold_c - shifted_mean) / forecast.stddev_c)
    ...
```

We use both methods and warn if they diverge by >5pp — that's a signal that the ensemble is bimodal and σ-based math is misleading.

## Validation flow (using METAR live)

Every 15 minutes during the trading day:

1. Pull METAR latest reading for each open-trade station.
2. Compare `metar_current_temp_c` against the morning's `p50_c + bias_c` for today.
3. Display the running gap in the dashboard. If `current - forecast_max > +3°C` at noon (already exceeded forecast max), flag the trade as **likely loser** if we were YES on "below X" or **likely winner** for "above X".

This is **diagnostic only** — we don't change the entry/exit logic mid-day. Resolution is still driven by Polymarket's official outcome.

## What to display in alerts

Every entry alert includes the bias for transparency:

```
Bias used: +1.5°C (14d, 12 samples, σ=0.8°C)
```

This lets the user see when the bias is well-measured vs noisy.

## Edge cases

- **Cold winter days**: bias may go negative (airport runway emits stored heat slower in cloud cover). Allow signed bias.
- **New station**: `sample_count=0` → `bias_c=0` (no correction). Pricer notes "bias n/a".
- **Stale data**: if `last_updated > 36h ago`, recompute on next job cycle even before the daily slot.
- **Outliers**: if `|bias_c| > 8°C`, mark as `notes='outlier'` and don't apply (probably a data bug).

## Future enhancements (not for V1)

- Per-month bias (city heat patterns differ by season).
- Hour-of-day bias for sub-daily markets.
- Use ECMWF MOS-corrected output (when available) and drop the bias entirely.
- Bayesian update instead of plain mean (stable bias from few samples).

## Why this works

Two consultations with the Polymarket Weather Assist webpage data (the existing project we're extending) showed that for **KBKF Denver**, the airport METAR daily max is on average +1.2°C above the Open-Meteo P50 over 30 days. That gap is large enough that a +1°C correction moves bucket probabilities by 5–10pp on close calls — exactly the range our `min_edge_pp` operates in.

A 14-day rolling window is short enough to track seasonal drift, long enough to denoise day-to-day variance (σ of the per-day bias is typically ~1.5°C; the mean over 14 days has σ ~0.4°C — comfortably below the 1°C scale of our bucketing).
