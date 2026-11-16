"""Nearby stations around each settlement station — stage 1: collection.

On Jul-Nov history (IEM archive), a model that also sees what the stations
around the settlement station are doing — how many are already falling from
their own high, what the upwind one did in the last hour — predicted
"will today's maximum still rise" 8.5% better (log-loss, 95% CI excluding
zero, every hour 11-16, AUC 0.945 -> 0.955), and a "buy only when the max is
locked" rule built on it came out +1.4 to +1.7 c/share on Oct-Nov prices
where the same rule without neighbours lost. Not yet significant (t ~ 1), so
the plan is staged: collect (this package), then a confident-buy strategy
traded on paper, then the dashboard and Telegram.

This stage only stores the neighbours' METARs in metar_observations, the
table the bot already keeps for its own stations. Nothing in trading reads
the neighbours.
"""
