"""Shadow study: does the model know something the market does not — and when?

An analysis of 2,260 settled trades found the model adds no information beyond
the market price *within the slice the bot trades* (stated confidence 85-97%).
That slice is too narrow to answer the real question, and the data only ever
recorded the model's view on buckets that crossed an alert threshold.

This package records, every hour, the model's estimate and the market price for
EVERY bucket of every open market, tagged with the city's own clock — hours
until the local day closes, local hour, and how fresh the newest forecast is.
Once markets resolve, that answers questions the trade log cannot:

  - at which point before close (if any) the model beats the market
  - whether the market moves toward the model afterwards (a speed edge)
  - whether a fresh model run briefly gives the model an advantage

Isolation is the design constraint. The package:

  - writes ONLY to its own tables (shadow_snapshots, shadow_market_state),
    which carry no foreign keys, so they can never constrain or block a
    production write or delete
  - never makes an HTTP call — prices come from the market_prices table the
    price job already fills, not from the CLOB
  - never calls _collect_outcome_data or _persist_collector_misses, the two
    production paths with side effects
  - never changes a trading decision; the detector does not know it exists

It reuses the production pieces rather than copying them — SignalAggregator,
estimate_with_breakdown, normalization_scale, the bucket-unit conversion — so
the number it records is the number production would compute.
"""
