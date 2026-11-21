"""The real track: the bot's own signals, filtered by the rule that held up on
May–November history — the event is TODAY in the city's own calendar and the
bot's confidence is at least 91%.

Every such signal is recorded once per bucket and side as a virtual buy at the
live price, and announced on Telegram with its own header, so it can be told
apart from the research alerts and, if wanted, bought by hand.

It runs NEXT TO the research track, never instead of it:
- the detectors only call `hook.note()` — an in-memory append that never
  raises — just before their own duplicate check, so an older research
  position on the same bucket cannot hide a same-day signal;
- the buys live in their own table (realtrack_buys), so no existing
  statistic, calibration or suspension rule ever counts them.
"""
