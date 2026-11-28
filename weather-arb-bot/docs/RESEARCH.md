# Research log

The bot's memory of what was tested, how, what came out, and what was decided.
Code and PRs say *what* the bot does; this file says *what we learned*, including
everything that did not work, so it is not tested again from scratch.

**Rules for this file**

1. **Read it before starting any new research or strategy.** Check whether the idea
   (or a close cousin) is already here.
2. **Every finished piece of research gets an entry, in the same PR** — including
   null results and abandoned ideas. A measured "no" is worth as much as a "yes".
3. Each entry: date · question · data and period · method · result **with the
   numbers as measured** · decision · status · where the code/script is.
4. Do not round, upgrade or soften numbers. If a result was found by slicing the
   same data it is tested on, say so.
5. Scripts that produced a result go in [`research/scripts/`](research/scripts/README.md).

Statuses: **adopted** (in the bot) · **rejected** (measured, did not work) ·
**open** (promising, not proven) · **parked** (not pursued, for a stated reason) ·
**superseded** (replaced by a later entry).

---

## 1. What we know (as of 2026-11-28)

The short version. Each line points to the entries behind it.

1. **The market is well calibrated at the mid.** A bucket priced at X¢ wins about
   X% of the time — at every price band, lead time and distance from the
   forecast that was checked. (R-2026-11-27b, d, e; earlier history §4)
2. **Taking liquidity costs ~2–3.5¢ per share** (median half-spread 2¢ at
   15–70¢, 3.5¢ at 70–85¢; much wider at fast moments). Most edges we found were
   smaller than that. (R-2026-11-27f; §4)
3. **The bot's own forecast does not beat the market.** Buying wherever the model
   says "more likely than the price" loses at every lead time and threshold, for
   the current model and for model v2 (t −2 to −9). (R-2026-11-27c)
4. **The bot's stated confidence is inflated:** "91–92%" won 74%, "95–96%" 78%,
   "97%" 83% (same day); live Real track "~93%" won 66%. A NO at "97%" is just
   the 3% probability floor. (R-2026-11-21a, R-2026-11-27a, R-2026-11-27h)
5. **Same-day ≥91% looked profitable on May–Nov positions (+3.0¢, t=2.39) but did
   not hold up:** live it made −1.6¢ (107 settled), and on November's shadow
   data the same rule made −1.7¢. (R-2026-11-21a → 27a → 27b)
6. **The money in these markets is made by speed and automation.** The most
   profitable consistent wallet is a bot that buys within ~1 minute of each new
   ECMWF run; another buys NO on buckets already ruled out by the measured
   temperature at 90–97¢ and won 179/179. Copying them an hour later earned
   nothing in the second half of November. (R-2026-11-27g)
7. **Real but fleeting mispricings exist:** after a 20¢+ drop in 15 minutes the
   price recovers within minutes (+2.1¢ after half-spread at 1 minute, ~0 at 5).
   Too fast for manual trading and the book is thin. (R-2026-11-27f)
8. **Edges decay.** Things that worked for a period stopped (the copy-trade window
   closed within November; city-level results do not persist from one period to
   the next). Any rule must be re-checked on new data.
9. **Settlement:** with the correct station, METAR (hourly + SPECI, rounded) matches
   the settled bucket on 98.7% of days. Hong Kong settles on HKO headquarters,
   not the airport the bot reads (15% match) — not fixed. (§4)
10. **Information the market does not have yet is the only edge with evidence:**
    nearby stations lead the market by ~10–30 minutes (paper test live since
    #135). On 26.11 the market was faster than the neighbours in Asia. (R-2026-11-26a)

---

## 2. Methods and pitfalls (read before measuring anything)

- **Score at the price you could actually trade.** Buying NO costs `1 − YES bid`;
  buying YES costs the YES ask. Results "at the mid" overstate by the half-spread.
- **Cluster the t-statistic by market** — buckets of one market are not independent.
- **Split by time** (first half / second half, or months) and report both halves.
  A result that lives in one half is not a result.
- **Slicing finds noise.** With ~60 cells, a few will show |t| > 2 by chance. Say
  when a rule was found on the same data it is reported on, and test it forward.
- **Small samples:** 58 trades at ±5¢ cannot distinguish +5¢ from −2¢.
- **Look-ahead (made twice):** joining shadow prices on `local_hour` without the
  same day picked prices from earlier days and produced a fake +10–16¢. Join on
  the exact snapshot (same timestamp), or require `hours_to_close < 24`.
- **Local calendar:** "same day" means the city's local date. The detector uses
  the server (UTC) date for `days_ahead`; for the rules tested it made little
  difference (912 vs 928 trades, +3.3¢ vs +3.0¢), but it shifts which markets are
  scanned around UTC midnight.
- **pandas traps hit in this repo's research:** `df.size` is the cell count, not a
  column named `size`; parsed timestamps can be in microseconds, so convert with
  `(ts - epoch) // pd.Timedelta('1s')`, not `astype('int64') // 10**9`.
- **Check what a price field means** before using it (CLOB history `p` is the mid,
  checked against the hourly book: inside bid–ask 89%, median |p − mid| 0.05¢).
- **Data sources and quirks:** see [`research/scripts/README.md`](research/scripts/README.md).

---

## 3. Entries — November 2026 (this phase)

### R-2026-11-21a · Same day vs days ahead, by confidence
- **Question:** Is the mistake spreading over too many days ahead? Does a higher
  confidence floor help?
- **Data:** positions export, 30.5–3.11.2026, 3,874 virtual buys (all NO), lead in
  the city's local calendar; entry at the ask.
- **Result:** same day 1,259 buys +1.0¢/share (+$61.6); 1 day −1.6¢ (−$92.1);
  2+ days −1.3¢ (−$96.2). Same day ≥91%: 928 buys, won 76.4%, paid 73.4¢,
  **+3.0¢, t=2.39**, positive in 4/4 quarters (+1.4/+4.2/+2.0/+5.3); every floor
  from 91 to 95 positive in 4/4 quarters; ≤90%: −4.8¢. One day ahead ≥91%:
  896 buys, −2.3¢, negative in all quarters. Win rate rises with confidence on
  the same day (62%→83%) but the price rises with it, so the edge does not.
  Per city: half-1 vs half-2 rank correlation −0.01 (noise); 33/48 cities did
  better same-day than 1-day.
- **Decision:** build the Real track (same day, ≥91%) as a separate, virtual
  track (#141) rather than change the research settings (changing
  `max_days_ahead_for_alert` also slows price polling for 1–3-day markets).
- **Status:** superseded by R-2026-11-27a/b — did not hold up out of sample.

### R-2026-11-23a · Results arrived up to 1.5 days late
- **Finding:** `job_check_resolutions` ran every 24 h from the last deploy and only
  took markets dated before the UTC date. Six 22.11 markets settled on Polymarket
  between 15:21 UTC (22.11) and 09:15 UTC (23.11) were still "open" at 16:53 UTC.
- **Decision:** hourly, as soon as the city's local day ends; skill refresh held to
  once per 20 h; markets >3 days old retried at 00 UTC only (#142).
- **Status:** adopted.

### R-2026-11-23b · Google DeepMind datasets / WeatherNext 3
- **Finding:** the announced datasets (PARDIG precipitation, SatWeave satellite)
  are not useful for daily-max temperature. WeatherNext 3 has a station-trained
  2 m temperature head (~5 km, hourly runs) — potentially relevant — but needs an
  access request; real-time data is under experimental terms whose rules on
  trading were not found; 6-hourly runs land ~8 h after initialisation.
- **Status:** parked (user's choice). Next step if resumed: request access, ask
  weathernext@google.com about trading use, backtest on historical (CC BY 4.0) data.

### R-2026-11-26a · "No Nearby events today"
- **Finding:** not a bug. Replaying `app/nearby/signal.py` on that day's METARs:
  the neighbour condition fired in Tokyo (15:00 local), Seoul (17:00), Singapore and
  Kuala Lumpur (18:00), but the holding bucket was already at 95–99.95¢ — above
  the 30–85¢ band, so by design nothing was recorded. The market was faster.
- **Decision:** per-city "today" tally and status line on the Nearby tab (#143).
- **Scripts:** `13_`, `14_`. **Status:** adopted (the status line).

### R-2026-11-26b · 404 "No orderbook exists" flood
- **Finding:** dozens of tokens per 5-minute run had no CLOB book; none belonged
  to any open or recently closed temperature event (could not be identified from
  the token alone).
- **Decision:** rest such a bucket 1 h; one WARNING naming city/date/bucket (#143).
  **Open:** read that warning in the logs to find out what they are.

### R-2026-11-27a · Real track, live (21–27.11)
- **Data:** Real track CSV, 131 buys (all NO, 130 α), 107 settled.
- **Result:** won 66%, paid 67.9¢, **−1.6¢ ±4.4** (−$8.36). Price ≥70¢: 58 buys,
  81% won, +5.6¢ ±5.1 (+$16.20); price <70¢: 49, 49% won, −10.0¢ ±7.3. 44% of buys
  at 00:xx local (first scan of the new day); none after 13:00 local (by midday
  the price leaves no 15¢ edge). Conf 95–97 +7.8¢ vs 91–94 negative (but higher
  price too).
- **Decision:** no rule change; a Min price filter on the tab so the owner can
  apply a floor manually (#145).
- **Status:** the ≥70¢ slice was then tested on more data → R-2026-11-27b.

### R-2026-11-27b · Does alpha add anything beyond the price? (the 70¢ question)
- **Data:** shadow CSV 1–27.11, event-day live snapshots with spread ≤10¢; one
  entry per bucket at the first hour its NO cost 70–85¢. 2,309 buckets, 1,156
  markets.
- **Result:** all: won 76.0%, paid 77.7¢, **−1.74¢, t=−2.60**. Alpha ≥91% at that
  hour: 455, −2.87¢; alpha rejected: 1,854, −1.47¢. Strategy "α ≥91% and NO
  70–85¢": 558, −1.66¢ (t −0.99); with the bot's 15¢ edge gate: −3.61¢. Negative
  in both halves, in every price band, and every local-hour band.
- **Conclusion:** no long-shot premium at 70–85¢, and alpha does not filter. The
  live +5.6¢ on 58 buys was most likely luck.
- **Status:** rejected (Real track as a money rule). Script `02_`.

### R-2026-11-27c · Model v2 vs the current model against live prices
- **Data:** `/admin/shadow/v2-report` since 2026-10-05 (813,287 rows); buy where the
  model exceeds the price by >5/10/20¢, one entry per bucket at the first hour.
- **Result:** every window and threshold negative for both. Morning of the event
  day, edge>5¢: v2 −1.93¢ (t −4.24, 7,727 entries) vs current −1.48¢ (t −6.51);
  1 day before: −2.20 vs −1.58; 2 days before: −2.94 vs −2.84. v2 is not better.
- **Decision:** do not switch to v2 and do not build a more complex alpha. Better
  weighting of "who was closer" (what v2 does) did not help.
- **Status:** rejected.

### R-2026-11-27d · Buckets far from the forecast
- **Data:** shadow CSV, event day (first hour, 08:00) and the day before (12:00).
- **Result:** at every price band NO's win rate ≈ its price. 2+ levels away,
  first hour: 97–99.5¢ → won 98.5% at 98.5¢; 93–97¢ → 95.1% at 95.4¢; 85–93¢ →
  89.1% at 89.5¢; 70–85¢ → 74.7% at 77.9¢. Same picture at 08:00, the day before,
  both halves. Distance from the forecast is already in the price.
- **Status:** rejected. Script `03_`.

### R-2026-11-27e · Fading hourly price jumps
- **Data:** shadow CSV, consecutive hourly snapshots, event day and day before.
- **Result:** YES +10¢ in an hour → buy NO: +0.1¢ (2,275), next hour back only
  0.6¢. YES +20¢: −2.0¢ (t −2.2) — the jumps were right. Following drops: −2.9¢.
- **Status:** rejected. Script `03_`.

### R-2026-11-27f · Minute-level jumps
- **Data:** CLOB 1-minute history for 12,815 buckets, 1,165 markets, event days
  2–26.11 (local day); costs = median half-spread by price from the shadow book.
- **Result:** 16,485 moves of ≥10¢ in 15 min. Prices revert ~1.6–1.9¢ within
  30–60 min. **YES drop ≥20¢ → buy YES 1 minute later: +4.11¢ at mid, +2.13¢ after
  half-spread (t 3.92), +0.16¢ after a full spread**; 5 minutes later +0.15¢;
  10–15 minutes later negative. Half 1 +0.53¢, half 2 +3.57¢; without the three
  busiest cities +1.87¢; strongest above 50¢ (+6.26¢) and 00–10 local (+10.5¢,
  n=214, found by slicing). Up-jumps: no edge after costs.
- **Decision:** not pursued — needs an automated trader reacting within a minute,
  and the book is too thin (owner's call).
- **Status:** parked. Scripts `04_`–`07_`.

### R-2026-11-27g · What the profitable wallets do
- **Data:** data-api v2 weather leaderboard (week/month/all, top 300 each); the 13
  wallets active in November's temperature markets; 11,165 trades on settled
  markets; prices from the shadow CSV and the minute history.
- **Context:** several all-time "weather" leaders are general traders (sports,
  politics) — not relevant. Consistent leaders earn 0.5–4.5% on volume.
- **WrongBy1Degree:** +$29,036 on $129,765 bought (22.4%), +$15.8k / +$13.2k in the
  two halves. A bot: buys in ~1-minute bursts starting 07:20, 13:30, 19:40, 01:20
  UTC — 20–40 min after each ECMWF run is published — almost all **one day
  before** the event (lead 1: +$27.8k on $109.9k). 71% YES at ~29¢ (won 36%),
  mostly the forecast bucket or 1–2 levels **warmer** than the bot's forecast;
  the bot's own model valued its buys 5.8¢ *below* the price paid. Price +3.3¢
  an hour after its buys (partly its own impact).
- **HighTempTation:** NO only, same day 10:00–14:00 local, 80–97¢ on buckets the
  measured temperature had ruled out: 179/179 won, +$2,992 (9.3%).
- **Bilberry:** a bot buying at :10 past every hour, same-day nights; +7%,
  profit only on the event day.
- **Copying WrongBy1Degree 1 h later** (mid, −2¢ half-spread): +2.92¢ (t 1.75,
  719 markets); events 2–13.11 +6.44¢ (t 2.45), 14–26.11 **+0.03¢**; 1 day before
  +4.02¢ (t 2.15). Copying HighTempTation 1 h later: −1.92¢.
- **Conclusion:** the edge is speed on new information (model runs, observations),
  delivered by bots; the late-follower window closed during November.
- **Open next steps:** (a) paper-test buying the bucket of each fresh ECMWF run for
  tomorrow (the bot already detects model updates and polls Open-Meteo at :20/:50);
  (b) locks at ≥88¢ — first check the intraday alerts the bot recorded but did not
  buy (Intraday Positions export, "Lock" and "alert (WIN/LOSS)").
- **Scripts:** `08_`–`12_`.

### R-2026-11-27h · How the confidence percentages are computed (audit)
- **Alpha** (`app/analyzers/probability_estimator.py`): per-model Student-t (df 6,
  σ 4°F same day +0.5°F/day, blended with 1.3× ensemble spread, clamped 3–7°F,
  ×1.5 for open-ended buckets) around bias-corrected forecasts (14-day EWMA from
  day-ahead errors, default +1.5°F); models weighted 0.5–1.5 by **binary** hit rate
  (90 days, per city and lead) — closeness (MAE) is stored but unused; 70%
  GFS+ECMWF ensemble member counting (Laplace) + 30% models (ensemble weight
  down to 40% when models disagree by >3°F) + 10% Wunderground; nudges toward 50%
  near edges/straddles/missing sources; same-day multipliers (METAR trend, wind,
  PIREP, dew point, sea breeze) that were never validated; clip [3%, 92%];
  normalised across buckets. **YES ≤92%, NO ≤97% (the 3% floor).** No market price.
  A calibrated % is computed (120-day bands) but only displayed.
- **Beta:** per-city/model bias (shrunk n/(n+20)), MAE-based σ (floor 4°F), blocks
  |bias| >5°F, weights 0.7 hit + 0.3 closeness, then **60% beta + 40% market**.
- **Intraday:** a different engine — running max + remaining forecast heating ×
  share of the day left (10:00→17:00), σ by hours to 17:00 (2.2→0.4°F; 0.3 after a
  confirmed peak), locks only when Wunderground confirms with a full-unit margin,
  cap 96% unless locked (98.5%). Buys need ≥94%, entry 70–88¢.
- **Real track** reuses alpha/beta's raw %. **Nearby** has no probability (a rule
  plus a fair-value-by-mid table). **Shadow v2** is research-only.
- **Ten weaknesses listed** (binary weights on 30% only, one global σ, thin tails,
  two bias systems, same-day ignores the running max, untested multipliers, no
  market input, calibration display-only, asymmetric clip, server date).
- **Decision:** not acted on (owner: keep it simple; v2 — which fixes several — did
  not beat the market, R-2026-11-27c).

### Smaller findings this phase
- **Clock "bug" (27.11):** Helsinki and Israel are both UTC+2 in winter; an alert at
  00:04 Helsinki was 00:04 Israel. Not a bug.
- **Real track flag bug (22.11):** "already held" fired when the regular bot bought
  the same bucket seconds later in the same run → now only positions opened
  before the signal; NO alerts now show NO bid/ask, not YES (#142).

---

## 4. Earlier history (May – mid-November 2026)

Recorded from the working notes kept before this file existed; the PR history
section below has the details and sources.

- **Database volume full (Railway, Nov):** recovered by moving to Pro and
  resizing to 10 GB; no data deleted. The owner is cost-sensitive (Railway
  ~$30+/month, driven by Postgres RAM).
- **Market behaviour:** calibrated at the mid; half-spread ~2–2.5¢ typically but
  ~7¢ median at signal moments and often 20–50¢; limit orders suffer adverse
  selection. Taker strategies were killed by the spread.
- **Stage 2 "confident buy" and model-vs-market approaches lose** — the market is
  better informed.
- **Shadow study (by time to close):** the bot's daily model's top bucket wins
  26%→31% as the event nears (Brier 0.075→0.071); the market favourite 33% → 44%
  → 47% → 50% → 61% → 98% at close (Brier 0.079 → 0.002).
- **Settlement:** with the right station, METAR (hourly + SPECI, rounding not
  truncation) matched the settled bucket on 98.7% of days. The earlier 21% US
  mismatch came from four wrong stations — fixed to KLGA, KHOU, KBKF, KDAL. Hong
  Kong settles on HKO headquarters while the bot reads VHHH (15% match) — open.
- **Nearby stations:** the market lags neighbours by ~10–30 min. Rule (≥2
  neighbours, ≥60% ≥1°F below their own high, mid 30–85¢, after the city's usual
  peak) + fair-value buy (ask ≤ fair − 2¢): **+8.4¢ out of sample (t=3.24, ~3.5 a
  day) on historical mids**; real asks at signal moments are often wide. 116 of
  134 neighbours useful in both periods. City-level results do not persist (rank
  correlation 0.0). Live paper test since #135–#140; judge at ~30 settled
  buyable signals.
- **Look-ahead mistake (twice)** — see §2.

<!-- PR-HISTORY -->
