"""When each city's daily high actually arrives — recorded, not yet used.

The intraday model assumes one peak window for every city and season
(14:00-17:00 local). Solar noon alone spans 11:41 (Tokyo) to 14:14 (Madrid)
on the local clock in summer, so at 15:00 one city is past its peak and
another has not reached it. This package measures the real peak time per
city and day from the METAR history, and summarises it per city and month.

Record-only. No trading code imports it (boundary test); wiring it into the
intraday estimator is a separate change, once the numbers show it helps.
"""
