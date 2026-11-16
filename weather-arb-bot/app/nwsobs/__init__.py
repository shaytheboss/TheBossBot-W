"""weather.gov station observations for US cities — recorded, not yet used.

Polymarket settles US temperature markets on the weather.gov timeseries,
which carries a reading roughly every five minutes. The bot reads only the
hourly METAR (plus SPECI) from aviationweather.gov, and since the stations
were fixed (24 Oct) the hourly maximum missed the settled bucket on 21% of
US market-days — 13% settled higher. Two things follow if the five-minute
series is what settles: the intraday model is reasoning from a coarser
thermometer than the market, and a new maximum appears there up to ~50
minutes before the next METAR.

This package only records that series (app/nwsobs/job.py) so the claim can
be measured against settled buckets and live prices. Nothing in trading
imports it.
"""
