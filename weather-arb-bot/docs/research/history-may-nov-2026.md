<!-- Extracted on 2026-11-28 from the descriptions of PRs #1–#145 and from
measured-result comments in app/, by an automated reading. Numbers are copied
from those sources; conflicts between sources are listed at the end. Summary and
current conclusions: ../RESEARCH.md -->

# Research log: findings and rules-with-reasons (PRs #1–#145, May–Nov 2026)

Sources: `prs_all.json` (145 merged PRs, dates are the PR **created** date) plus measured-result / incident comments in `weather-arb-bot/app/` (cited as `file:line`, dated "code").
Numbers are copied from the source. Where I derived something myself it is marked **(derived)**. "Illustrative" means the PR shows the numbers as an example layout, not as a measurement.

Status key: `adopted-in-code` (present in current `app/`), `rejected`, `open/unproven` (recorded or paper only, or never evaluated), `superseded-by:#n`.

Item format:
- **Q:** what was tested, or why the rule exists
- **Data:** dataset, period, n, method (only when the source states it)
- **Result:** numbers as written
- **Decision:** what was built, changed or rejected

Pure plumbing PRs (UI, CSV buttons, refactors, test-only) are skipped unless they encode a finding. Skipped: #1–3, 5, 8–11, 13–14, 34, 39 (dup of #38), 44, 49–50 (dups of #48, except the P&L bug), 53 (dup of #52), 66, 77, 79, 82, 92, 96 (dup of #95), 104 (dup of #103), 120, 126, 131, 138, 144.

---

## forecast-model

#### `fm-gfs-ensemble-primary` — GFS ensemble as primary probability source
2026-05-14 · PR #22 · **superseded-by:#25**
- **Q:** Get real probabilities from ensemble members instead of a weighted average of point forecasts.
- **Data:** Open-Meteo GFS ensemble, "~31 GFS runs".
- **Decision:** Ensemble used as primary when present; fallback = GFS + ECMWF + Wunderground weighted average. Also fixed: GFS collector always fetched today (`forecast_days=2`), aggregator did not filter by market date.

#### `fm-laplace-gaussian-blend` — Laplace smoothing + deterministic Gaussian, 70/30 blend
2026-05-16 · PR #25 · **adopted-in-code** (70/30 base and Laplace still present. The σ, the clip and the distribution were changed later in #28, #42, #46, #73)
- **Q:** Why "99% certainty NO" on buckets where GFS sits on the boundary?
- **Result:** Raw 0/30 → 0% → clip 1% → 99% NO. "0 hits in 30 trials on a 1-2°F bucket ... typical confidence interval is 0-12%." Re-computed: Atlanta 88-89°F NO (GFS=88, ECMWF=87, ens 0/30, median 87°F): 99% → 91.6% (Laplace 1.6% + Gaussian deterministic 24%). Dallas 92°F+ NO (GFS=91, ECMWF=92, ens 2/30): 93% → 79%, below 80%, so no alert. NYC 86-87°F NO (GFS=89, ECMWF=88, ens 0/30, median 83°F): 99% → 92.6%.
- **Decision:** Laplace `(hits+0.5)/(n+1)`. Deterministic N(forecast, "3°F²") via erf. Blend 70% ensemble + 30% deterministic. Wunderground as a soft 10% correction. Clip [0.03, 0.97].

#### `fm-more-deterministic-sources` — HRRR, Tomorrow.io, Meteosource; NWS date bug
2026-05-16 · PR #26 · **adopted-in-code**
- **Q:** Add sources. NWS was useless for future markets.
- **Result:** NWS `collect()` always matched `date.today()`.
- **Decision:** HRRR (3 km, CONUS only, max 2 days ahead), Tomorrow.io and Meteosource added. The deterministic Gaussian averages all models. External sources fetched every 4 h, 3 days ahead, "to conserve rate-limited API quota".

#### `fm-icon-collect-only` — DWD ICON collected but not blended
2026-05-27 · PR #40, #41 · **superseded-by:#42**
- **Decision:** ICON via Open-Meteo `icon_seamless`, stored as `source="icon"`, kept out of `_DET_SOURCES` "after a calibration period". The job was registered only in the standalone `scheduler.py`. See `sd-icon-tio-hk-root-causes` (#88): it never ran in production until 2026-07-11.

#### `fm-student-t-icon-e2-e3-normalization` — Student-t, ICON 7th source, dew/gradient signals, market normalization
2026-05-29 · PR #42 · **adopted-in-code** (all present in `probability_estimator.py` / `opportunity_detector.py`)
- **Q:** Reduce overconfidence near edges; use more signals.
- **Decision:** Student-t(df=6) replaces Gaussian for deterministic sources. ICON joins `_DET_SOURCES`. E2: same-day T−Td < 5°F → P(warm bucket) × 0.92. E3: primary station > 8°F warmer than reference → × 0.91. Market normalization: all outcomes' probabilities rescaled to sum = 1. Confidence scorer: 8 sources, +10 when 4+ agree, +20 when today's METAR max is inside the bucket, +10 when it has passed the floor. No measurement is given for any of these multipliers or bonuses.

#### `fm-airport-warm-bias` — +1.5°F station warm-bias correction
2026-05-30 · PR #48 (commit 67a8c33) · **adopted-in-code** (`bias_estimator.DEFAULT_BIAS_F = 1.5`; modified by #62, #72, #73)
- **Q:** Model forecasts systematically under-estimate METAR daily highs.
- **Result:** Default +1.5°F, "learned from 14-day rolling window".
- **Decision:** Shift every forecast up by `+bias_f` before the CDF.

#### `fm-per-source-bias-city-weights-dual-ensemble` — methodology upgrades
2026-06-09 · PR #62 · **adopted-in-code** (ensemble σ floor later raised to 3.0 by #73)
- **Decision:**
  - per-source bias (GFS≠HRRR≠WU), preferred over the overall bias;
  - per-city model weights from realised hit-rate vs Polymarket's settled bucket: neutral until 5 samples, bounded 0.5–1.5, 10-min cache;
  - σ from the actual ensemble spread, clamped 2–7°F, needs 10+ members;
  - ECMWF ensemble (~50 members, `ecmwf_ifs025`) merged with GFS;
  - model accuracy de-duplicated per (market, model, detection day).
- **Deferred at the user's request:** auto-calibration, double-exposure prevention, Kelly sizing. The first two were built later (#63, #72). Kelly was never built: no "kelly" anywhere in `app/`.

#### `fm-model-skill-db` — managed per-city model-skill table
2026-06-12 · PR #70 · **adopted-in-code**
- **Q:** Which model is actually right in each city, judged only against Polymarket's result?
- **Data:** each model's "last word" on the event day vs the Polymarket winning bucket. 90-day rolling window.
- **Decision:** `model_skill` per (city, model) stores samples, hits, smoothed hit-rate, MAE, signed bias and a weight in [0.5–1.5]. Neutral below 5 samples. The weight formula is bit-identical to the old one. Used by the daily deterministic average and as intraday blend multipliers, giving "up to 3× influence (1.5 vs 0.5)". Wunderground is deliberately not scored, because on the event day its "forecast" is an observation.

#### `fm-skill-lead-time` — skill tracked per days_ahead
2026-06-12 · PR #71 · **adopted-in-code**
- **Q:** A 2-day-ahead alert was being weighted by same-day accuracy.
- **Decision:** `days_ahead` column with key (city, source, days_ahead). Leads up to `MAX_DAYS_AHEAD = 3` (code).

#### `fm-wu-anchored-bias` — bias measured against WU, not METAR
2026-06-13 · PR #72 · **adopted-in-code** (premise questioned by #129, see Conflicts)
- **Q:** The bias was trained as mean(METAR max − NWP T-1), but Polymarket resolves on WU, which "runs 1-2°F warmer than METAR".
- **Result:** The correction was "systematically too cold → the bot overconfidently bet NO on hot buckets".
- **Decision:** With ≥ 5 days of WU history, bias = mean(WU_observed_high − NWP_T1_forecast). Otherwise fall back to METAR.

#### `fm-ewma-bias` — recency-weighted bias for heat waves
2026-06-13 · PR #73 · **adopted-in-code** (`BIAS_RECENCY_HALF_LIFE_DAYS = 5.0`, `BIAS_REGIME_SHIFT_F = 1.5`)
- **Q:** A flat 14-day mean barely moved during a heat wave.
- **Result:** Check case: today 3°F and an older day 0°F, one half-life apart → EWMA 2.0°F (2:1 weights). The "4°F above the mean for 5 days" heat wave is a hypothetical example.
- **Decision:** EWMA with a 5-day half-life. A regime warning fires when the last 5 days diverge from the window mean by > 1.5°F.

#### `fm-beta-estimator` — parallel "beta" calibrated estimator
2026-06-19 · PR #78 · **open/unproven** (runs in code. Its measured results were worse than alpha: see `cc-beta-anti-predictive`)
- **Q:** A per-city calibrated estimator, run beside alpha without touching it.
- **Result (claims, no dataset given):** NY reverse bias, where alpha's +1.5°F correction made things worse. LA ECMWF bias +6.7°F. Tokyo Meteosource weight 0.767 (alpha) → 0.942 (beta). Code adds "Tokyo Meteosource: MAE=0.91°F, weight=0.767 in alpha" (`beta_estimator.py:278`).
- **Decision:**
  - bias from `model_skill.bias_f`;
  - σ = max(3°F, mae×1.2);
  - |bias| > 5°F → source excluded;
  - weight = 0.7×hit_rate + 0.3×accuracy;
  - "variance city" (London archetype) → σ × 1.2.

#### `fm-beta-skill-aggregation` — beta skill pooled across lead times
2026-06-26 · PR #84 · **adopted-in-code**
- **Q:** Why beta wasn't learning.
- **Result:** One week of history split over 4 lead-time buckets × 7 models × 48 cities gives "roughly 1–2 samples per cell", all below MIN_SAMPLES = 5. Beta therefore ran as a neutral clone of alpha. "With 30+ resolved markets per city since launch" (claim).
- **Decision:** When the exact lead slot is thin, aggregate all leads into a `_SkillSnapshot` (weighted-average MAE and bias).

#### `fm-beta-residual-sigma` — MAE split into bias + residual for σ
2026-06-26 · PR #85 · **adopted-in-code**
- **Q:** Beta corrected the bias and then also widened σ by the same raw MAE, counting it twice.
- **Result:**

  | case | n | mae | bias | σ before → after (°F) | why |
  |---|---|---|---|---|---|
  | Ankara ECMWF | 37 | 4.0 | -4.0 | 4.85 → 4.29 | clean bias, 65% corrected |
  | London ECMWF | 85 | 4.11 | +0.36 | 4.95 → 4.94 | bias ≈ 0 |
  | KL ECMWF | 10 | 3.0 | -3.0 | 4.11 → 4.57 | shrink 0.33, most bias uncorrected |

- **Decision:** σ_resid = √max(0, mae² − b_corrected²). Floor 4.0 and cap 7.0 unchanged.

#### `fm-model-v2-shadow` — per-city bias-corrected, accuracy-weighted model (record-only)
2026-10-04 · PR #129 · **open/unproven** (recorded in the shadow study, not traded; `app/shadow/v2.py`)
- **Q:** Does per-city/model/lead bias correction plus accuracy weighting beat the current forecast?
- **Data:** Jun–Oct, the 37 cities whose station matches the market's. Bias and error learned per city and lead from the previous 21 days only (no look-ahead).
- **Result:** Right bucket 42% vs 34% one day ahead; 47% vs 39% on the event day. Against live Polymarket prices, the first positive version: +1.7 to +4.7 ¢/share on the morning of the event day, "over only eight days and nine configurations".
- **Decision:** Recorded as `v2_p` / `v2_mu` and judged only on days after recording starts (`/admin/shadow/v2-report`, t clustered by market). Spread learned per city ×1.25, floor 0.9°F. Code warns: where the METAR station ≠ the resolution station, v2 learns the wrong bias.

#### `fm-model-run-values` — record each model run's value
2026-11-05 · PR #132 · **open/unproven** (record-only)
- **Q:** Which run of each model is most accurate per city (e.g. ICON 00z vs 12z)? How many minutes does the market take to react to a new run?
- **Data:** Estimated from the "when models update" report: ~3,000 city-level changes a day × up to 4 dates.
- **Result:** Estimated ~10k rows/day, ~1 MB/day, ~60 MB at 60-day retention.
- **Decision:** `model_run_values` stores the unrounded high whenever it changes, for dates from yesterday to +2 days. Nothing in trading reads it. No analysis reported yet.

---

## calibration/confidence

#### `cc-directional-certainty-80` — side chosen by certainty, 80% gate
2026-05-14 · PR #21 · **superseded-by:#33** (split thresholds; near alert later back to 0.80 in #88)
- **Q:** Choosing the side by edge alone "could fire when we were only 55%".
- **Decision:** side = YES if p ≥ 0.5. certainty = max(p, 1−p). edge = certainty − market price of that side. An alert needs certainty ≥ 80% (was 60% on the signal-quality `compute_confidence`) and edge ≥ `min_edge_for_alert`.

#### `cc-boundary-proximity` — May 20 retrospective: losses sit on bucket edges
2026-05-21 · PR #29 · **adopted-in-code** (`BOUNDARY_WINDOW_F = 1.5`, `BOUNDARY_MAX_BLEND = 0.25`)
- **Data:** settled bets of May 20.
- **Result:** 26 wins / 5 losses (83.9%). All 5 losses landed within ≤1°F of a bucket edge:

  | bet | actual |
  |---|---|
  | London 20°C NO @ 42¢ | 21.0°C |
  | Seattle 66-67°F NO @ 38¢ | 66.0°F |
  | Miami 88-89°F NO @ 34¢ | 88.0°F |
  | SF ≥76°F YES @ 22¢ | 75.9°F |
  | Houston 82-83°F NO @ 37¢ | 82.9°F |

  Wins were dominated by NO @ 10–25¢ on clearly out-of-range buckets. "Market prices of 30-42¢ already reflected that uncertainty; we ignored it."
- **Decision:** When the forecast is within 1.5°F of an edge, blend toward 50% with a weight rising linearly to 25%. Kept: the liquidity gate (#27), lead-time σ (#28), the 70/30 blend and same-day-only observations.

#### `cc-uncertainty-overhaul` — disagreement, closest-source boundary, straddle
2026-05-23 · PR #31 · **adopted-in-code** (`SOURCE_SPREAD_THRESHOLD_F = 3.0`, `ENSEMBLE_WEIGHT_MIN = 0.40`, `STRADDLE_EXTRA_BLEND = 0.10`)
- **Decision:**
  - source spread > 3°F → ensemble weight cut linearly from 70% to 40% at ≥ 6°F;
  - the boundary blend is triggered by the single closest source, not the average;
  - sources on both sides of an edge add up to 10% extra blend toward 50%;
  - bucket bounds use the convention [min, max+1).

#### `cc-split-thresholds` — near/far alert and buy thresholds; 5-share virtual buys
2026-05-25 · PR #33 · **superseded-by:#88** (near alert 0.75 → 0.80)
- **Decision:** Alert near (≤ 1 day) 0.75, far 0.80. Buy near/far 0.90/0.90, with buy ≥ alert. Virtual positions of 5 shares at the side's ask.

#### `cc-sparse-source-shrink` — fewer sources → blend toward 50%
2026-05-27 / 2026-05-30 · PR #41, #48 (dups #49, #50) · **adopted-in-code** (`_SPARSE_SOURCE_BASELINE = 5`, `_SPARSE_SOURCE_SHRINK_PER_MISSING = 0.08`)
- **Q:** São Paulo / Singapore overconfidence (#41). Seoul P(NO) = 97% next to a HIGH risk label (#48).
- **Result:** The baseline was 3 total sources. Seoul with 3 global sources (GFS + ECMWF + Meteosource) hit it exactly, so zero shrinkage "despite 4 missing sources".
- **Decision:** #41: ≤ 2 deterministic sources → shrink. #48: baseline 5 global sources (HRRR/NWS marked CONUS-only), 8% per missing source. Seoul 3/5 → 16% shrink → P(NO) 97% → ~89%.

#### `cc-discretization-and-ci` — discretisation σ and ±pp display
2026-05-27 · PR #41 (Wave 1), #42 · **ambiguous** (the ±pp CI display is adopted. `DISCRETIZATION_SIGMA_F = 0.289` is not found in current `app/`, and no PR records its removal)
- **Decision:** σ combined in quadrature with 0.289°F (std of uniform[−0.5, +0.5]): same-day σ 1.5 → 1.528°F. The CI shown is "96% ± 5pp", computed from the source spread.

#### `cc-lessons-first-pass` — first calibration post-mortem
2026-05-29 · PR #45 · **adopted-in-code** (Lessons tab)
- **Data:** "first manual pass over 3 days of data", ~59 settled bets.
- **Result:** ~61% real win-rate vs ~95% stated confidence. Almost all bets were NO on favourites at 50–84¢ (break-even = price), so the strategy was "net-negative despite 'mostly winning'".
- **Decision:** Lessons tab: calibration gap, Brier, and real edge = win-rate − average entry, broken down by band, bucket shape, side, lead, entry band and city.

#### `cc-clip-92-sigma-open-ended` — lower clip, wider σ, open-ended buckets
2026-05-29 · PR #46 · **adopted-in-code** for clip 0.92 and open-ended ×1.5. σ **superseded-by:#73**.
- **Data:** 3 days, ~100 opportunities.
- **Result:**
  - a ~35pp calibration gap: stated ~95% ("nearly everything was clipped at 97%") vs real ~61%;
  - open-ended buckets ~4/13 wins at 97%;
  - the entire 05-26 19:29 batch sat at exactly 97%;
  - "Real NWP MAE for daily highs is 2-3°F same-day" (claim, no source).
- **Decision:**
  - `_PROB_CLIP_HI` 0.97 → 0.92;
  - σ = min(5.5, 2.5 + 0.5·d), i.e. 1.5 → 2.5 same-day;
  - open-ended σ × 1.5 and always 🔴.
- **Predicted, not measured:** 2°C above threshold same-day ~97% → ~83%; 1°C ~97% → ~76%; gap "should narrow from ~35pp toward ~15-20pp". No later PR reports that the gap narrowed.

#### `cc-no-data-fake-prior` — no source must mean no trade
2026-05-30 · PR #47 · **adopted-in-code**
- **Result:** With no forecast source, the blend fell back to a flat p = 0.25. Normalization turned that into a fake high-confidence NO: Hong Kong 30°C/31°C showed P(NO) = 91% beside "No forecast data available". The headline 91% disagreed with the math section's 71.2%. Verification: no-data p = 0.287 with 7 missing sources.
- **Decision:** Skip outcomes with zero real sources. Record `missing_sources`. Show `normalized_final`. Also: the deterministic models (79–82°F) and the ensemble median (86°F) split around the Atlanta 84–85°F bucket → "direction split" warning.

#### `cc-confidence-bands-real-data` — win rate by confidence band
2026-06-07 · PR #59, #60, #61 · **adopted-in-code** (display)
- **Result (#60, real):** 90–94%: 80 positions, 58 wins, 72.5%, ROI −3.1%. 95–100%: 9 positions, 9 wins, 100%, ROI +26.4%. The cumulative table had shown the same 89 positions in every 50–90% row, because positions only open at ≥ 90%.
- **Result (#61):** With Min conf 91 the panel showed net P&L +$8.05 but the bands summed to ~$1.20. The gap is two losing conf-90 positions, −$6.85.
- **Note:** #59's table (45 / 38 / 28 / 73.7% … 100% / +42.9%) looks illustrative.

#### `cc-empirical-calibrator` — band win-rate calibrator (display only)
2026-06-10 · PR #63 · **adopted-in-code** as display-only (`calibrator.py`: MIN_SAMPLES 8, LOOKBACK 120 days, MAX_BLEND_WEIGHT 0.60)
- **Decision:** 2-point bands, 120-day lookback, 30-min cache. Shown in the alert when the gap is ≥ 2pp. Thresholds still use raw certainty, "so 90–92% virtual-buy tracking is unaffected".

#### `cc-calibration-gate` — block buys when calibrated certainty < threshold
2026-06-13 · PR #72 · **rejected** (reverted the same day in #73. No reason is given in #73; #75 says the overconfidence is "left untouched by design")
- **Q:** "A 95% raw signal that historically wins only 70% of the time still triggered an automatic buy" (example).
- **Decision:** Buys needed calibrated ≥ buy threshold (band ≥ 8 samples, blend ≤ 60%). Reverted to display-only. `_calibration_gated` is still stored.

#### `cc-sigma-4F` — same-day σ widened to 4°F
2026-06-13 · PR #73 · **adopted-in-code** (`forecast_sigma_for_lead` = min(6.0, 4.0 + 0.5·d); `_SIGMA_BLEND_MIN_F = 3.0`)
- **Q:** σ = 2.5°F still produced confident losses.
- **Result:** "Summer daily-max errors run 3–5°F even same-day" (claim). Denver regression, forecast 88°F vs bucket 92–93°F: σ 2.5 → NO certainty 93.0% → auto-buy → loss; σ 4.0 → 90.2%.
- **Decision:**

  | lead | σ old → new (°F) |
  |---|---|
  | 0 | 2.5 → 4.0 |
  | 1 | 3.0 → 4.5 |
  | 2 | 3.5 → 5.0 |
  | 3 | 4.0 → 5.5 |
  | 4+ cap | 5.5 → 6.0 |

  The ensemble-blend σ floor goes from 2.0 to 3.0°F.

#### `cc-daily-overconfidence-jun` — June CSV review: overconfidence left in place on purpose
2026-06-19 · PR #75 · **open/unproven** (deliberately not acted on)
- **Data:** 320 daily positions, 30 May–17 Jun.
- **Result:** 100% NO bets, 71% win rate, −$37.96 net ("structurally selling tails"). "Systematic overconfidence (raw conf 90→57% true)". High edge (≥ 28pp) → 48% win rate. Double positions: zero after 13 Jun.
- **Decision:** Untouched. Virtual buys at 90–92% are kept for data collection; manual buying happens only above 93%.

#### `cc-beta-anti-predictive` — beta's top confidence was anti-predictive
2026-06-22 · PR #80 · **adopted-in-code** (beta only)
- **Data:** positions export, 662 rows.
- **Result:**
  - ROI: beta −13.4% vs alpha −5.0%.
  - Beta claimed ~90% → won 50%; ~94% → 73%; ~96% → 50%.
  - Bets at a 75¢ entry (market-implied 75%) resolved ~64% while beta claimed 95%.
  - Bigger edge → worse results.
- **Decision:**
  - bias shrink n/(n+20) (n=5 → 20%, 20 → 50%, 60 → 75%);
  - σ floor 3 → 4°F, plus mae/√n in quadrature;
  - blend toward the market mid with weight 0.6;
  - beta Telegram gate 0.85.
- **Simulated, not measured:** the classic loss (88°F vs a 92–93°F bucket) drops from ~0.93 to 0.80.

#### `cc-beta-starved` — the beta gates killed beta's data flow
2026-06-26 / 2026-07-11 · PR #83, #88 · **adopted-in-code** (`min_confidence_beta_alert = 0.80`, `min_confidence_beta_buy = 0.85`)
- **Result (#83):**
  - beta positions per day: Jun 19 37, Jun 20 47, Jun 21 41, Jun 22 32, Jun 23 0, Jun 24 1 (Wuhan 95%), Jun 25 0, Jun 26 0;
  - raw 90% at a 72¢ NO → 82.8% blended (0.6·raw + 0.4·market);
  - the blend "structurally cuts confidence by ~5–7pp";
  - 90–94% raw was "~80% of all beta trades" on Jun 19–22.
- **Result (#88):** beta went from 125 virtual buys/week to 1. The blend caps confidence at ~88%, so the 0.90 buy gate was unreachable.
- **Decision:** Beta alert gate 0.85 → 0.80 (#83). Separate beta buy gate 0.85 (#88).

#### `cc-confidence-jump-after-sources-fixed` — ~7-point jump when the sparse penalty vanished
2026-07-21 · PR #89 · **open/unproven** (explanation only; the penalty was not changed)
- **Result:** Before #88, ICON and Tomorrow.io were dead → 2 sources missing → 16% penalty. After the fix → 5/5 → no penalty → confidence "jumped ~7 points at once" (SF "α 96% NO 70-71°F"). Also a display bug: "7/5 global models" (HRRR and NWS counted against a global baseline of 5).

#### `cc-realised-78-at-90` — fleet realised rate at ≥90% stated
2026-09-23 · PR #108 · **observation** (used in the suspension analysis)
- **Result:** At ≥ 90% stated confidence the fleet's realised win rate is "around 78%". "The estimator is chronically overconfident, which is a separate problem."

#### `cc-display-risk-scorecard` — risk banner and decision scorecard (display rules)
2026-05-29 / 2026-05-30 · PR #43, #48 · **adopted-in-code** (display only)
- **Decision:** 🔴 when spread > 6°F, CI > ±20pp or < 0.5°F from an edge. 🟡 when spread 3–6°F, CI ±10–20pp, within 1.5°F, or straddle. #48 turned it into a 7-row scorecard (confidence, coverage, agreement, entry cost, boundary, bucket type, ensemble); overall = worst non-grey row. Missing sources split into: API key missing / no data / CONUS-only N/A.

---

## market-vs-model

#### `mm-edge-ceiling` — a very large edge means the model is wrong
2026-05-27 · PR #41 · **adopted-in-code** (`max_edge_for_alert = 0.45`)
- **Q:** Seattle: a +67pp edge was alerted as an opportunity.
- **Decision:** Edge > 0.45 is treated as a model-error signal ("the market knows something we don't").
- **Later support:** #75: edge ≥ 28pp → 48% win rate. #80: beta, bigger disagreement → worse.

#### `mm-shadow-study` — model adds nothing beyond the market inside the traded slice
2026-09-24 · PR #111 · **adopted-in-code** (record-only study, `app/shadow/`)
- **Data:** 2,260 settled trades, logistic regression, inside the slice the bot trades (stated confidence 85–97%).
- **Result:** market z = +6.28, model z = +0.32, p = 0.75.
- **Decision:** Every hour, record model vs market for every bucket of every open market: hours to local close, local hour, minutes since the newest forecast, number of sources.
  - Skip buckets both sides call dead (model < 2%, market < 3%). Keep 45 days.
  - Measured 152 bytes/row with every index (100,000 rows, PG16).
  - ~29 queries per eleven-bucket market instead of ~198.

#### `mm-shadow-live-book` — compare against the live order book
2026-09-24 · PR #112 · **adopted-in-code**
- **Q:** `market_prices` is written on change, so a row's timestamp is the last change, not the last check. The mid is not what you pay.
- **Decision:** One read-only book GET per recorded bucket, with a `price_live` flag and `price_job_age_min`. Cost: "roughly 2% on top" of existing CLOB calls.

#### `mm-shadow-post-close` — post-close rows reversed a verdict
2026-09-25 · PR #117 · **adopted-in-code**
- **Result:** Guangzhou 24 Sep: the model had the winning bucket (35C) from 11 h before close, the market from 8 h. Seven post-close rows had the model on 34C, so the summary said "model never settled, market led by 8h". The same rows inflated the market's Brier score and average probability.
- **Decision:** Skip once `hours_to_close <= 0`; summaries use pre-close snapshots only.

#### `mm-daily-model-cannot-see-thermometer` — late-day comparison was unfair to the model
2026-09-25 · PR #118 · **adopted-in-code**
- **Result:** The daily model's minimum spread of 4°F "caps any single 1°C bucket at about 20%". Guangzhou 24 Sep: the market jumped to 96% at 16:00 while the daily model stayed at 19%.
- **Decision:** Record the intraday model's `intraday_p` beside the daily one, using the same helpers as the detector. Score both against the market on intraday hours only.

#### `mm-compare-picks-not-probabilities` — judge favourites, not probabilities
2026-09-28 · PR #125 (after #122) · **adopted-in-code**
- **Q:** The model spreads probability across buckets while the market price runs to 100% as the day is decided: "'29% vs 100%' doesn't mean the model was wrong."
- **Decision:** Show each side's favourite bucket hour by hour. "Hours on the winning bucket" replaces the Brier lines. The #122 checkpoint grid (48/24/12/6/3/1 h) was replaced.

---

## pricing/spread/liquidity

#### `pl-midpoint-illiquid` — price against the book, not the midpoint
2026-05-17 · PR #27 · **adopted-in-code**
- **Result:** NYC: the bot showed "NO 86¢" while the market was really at 98¢ ($1 volume), because edge was computed from `/midpoint`.
- **Decision:** Read `/book`. Skip outcomes with no two-sided book or a spread > 10¢. Edge vs real entry cost: NO = 1 − yes_bid, YES = yes_ask. The DB still stores the YES mid.

#### `pl-closed-market-gate` — empty book passed the spread gate
2026-05-22 · PR #30 · **adopted-in-code**
- **Result:** bid = 0, ask = 0, spread = 0 passed `spread > 0.10`.
- **Decision:** Skip when bid == 0 and ask == 0. Also skip `days_ahead < 0`.

#### `pl-no-side-price-display` — NO bets displayed at the complement
2026-05-25 · PR #33 · **adopted-in-code**
- **Result:** `market_price` stores the YES mid, so NO bets were shown at 20–40¢ while the real NO ask is "typically 60–95¢". This matched the user's suspicion that "we always bought at 30-40¢".
- **Decision:** Side-aware display. Store `virtual_entry_price` = the exact ask.

#### `pl-break-even-rule` — break-even win rate = price paid
2026-05-29 · PR #45; code `app/config.py` (suspension comment), `app/analyzers/opportunity_detector.py:146` · **adopted-in-code** (Lessons and research metrics)
- **Rule:** Real edge = win rate − average entry price. "Breakeven at a 75¢ NO entry is 75% win rate."

#### `pl-intraday-max-entry-88` — no intraday buy above 88¢
2026-06-13 · PR #74 · **adopted-in-code** (`intraday_max_entry_cost = 0.88`)
- **Q:** "Buying YES >88¢ is −EV."
- **Result (arithmetic, not measured):** `config.py`: "risking $4.40 to win ~40¢ — even at 99% win rate the EV is only 3¢ but one lock-failure loss destroys 10 wins". `detector.py:696`: "At 92¢ entry we risk $4.60 to win $0.40 ... wipes out 11 wins".
- **Decision:** Buy blocked above the cap; alerts still fire.

#### `pl-basket` — multi-bucket NO basket
2026-06-19 · PR #76 · **adopted-in-code** (intraday; no later evaluation reported)
- **Result:** Warsaw 16/6: 4 NO bets at an average of ~55¢ → 3 won → +$2.05.
- **Rule:** Exactly one YES wins. Net EV/share = (N−1)/N − average entry, so 3 legs are profitable below 0.667 and 4 legs below 0.750. Basket when ≥ 3 NO legs qualify and EV > 0.

#### `pl-owner-manual-rule` — manual buy rule (not in code)
2026-06-23 · PR #81 · **open/unproven** (owner's manual rule)
- **Rule:** "Buy only when σ ≥ 1.6°F and entry ≤ 78¢". Evidence in #81: σ ≥ 1.6°F trades made +$18.96 (see `id-unconfirmed-lock-99`).

#### `pl-intraday-min-entry-70` — no intraday buy below a 70¢ ask
2026-09-24 · PR #113 · **adopted-in-code** (`intraday_min_entry_cost = 0.70`; `detector.py:702`)
- **Data:** 5,418 settled intraday bets, time split.
- **Result:**
  - ask < 70¢: Jun–Jul −6.35pp (t = −3.48); Aug–Sep −5.27pp (t = −2.77);
  - skipping them cut the later half's loss by ~⅔, at the 99th percentile against random subsets of the same size;
  - those bets won 50–58%;
  - "The only rule in this investigation that survived an out-of-sample test";
  - "It avoids losses, it doesn't create an edge": even with the rule the later half is −0.95pp.
- **Decision:** Gates the virtual buy only; alerts still fire. 0 = off.

#### `pl-no-two-sided-book-no-trade` — the detector refuses one-sided or crossed books
2026-09-24 · PR #112 · **adopted-in-code** (behaviour confirmed)
- **Result:** Two tests had passed without ever receiving the bad book. Given one, the detector does not trade that bucket ("no ask → no executable price → no trade"). Another bucket stays exactly at 83%.

#### `pl-realtrack-side-book` — NO book prices
2026-11-22 · PR #142 · **adopted-in-code**
- **Result:** The Jeddah NO 35°C alert and tab showed YES prices on a NO bet.
- **Decision:** NO bid = 1 − YES ask; NO ask = 1 − YES bid.

#### `pl-realtrack-70c-floor` — real-track buys below 70¢ lost
2026-11-27 · PR #145 · **open/unproven** (filter only; the owner applies it by hand; no rule changed)
- **Data:** live Real track export.
- **Result:** ≥ 70¢: 58 settled, 81% won, +5.6¢ per share. < 70¢: 49 settled, 49% won, −10.0¢ per share.
- **Decision:** Min price ¢ filter (Real track) and Min ask ¢ filter (Nearby).

---

## settlement/stations/data-sources

#### `sd-nyc-knyc` — NYC resolves on Central Park
2026-05-14 · PR #15, #16 (reverses #4's KLGA) · **superseded?** (see Conflicts: the current `app/nearby/stations.py`, generated 2026-11-16, keys New York's settlement station as KLGA with KNYC as a neighbour; `app/utils/seed.py` still says KNYC)
- **Q:** "Polymarket NYC weather markets resolve via Central Park (KNYC), not LaGuardia."
- **Decision:** primary_icao KNYC, reference KLGA. Seed matches cities by name.

#### `sd-discovery` — how to find Polymarket temperature events
2026-05-14 · PR #17–#20 · **adopted-in-code** (final form)
- **Result:**
  - Gamma `?q=temperature` "does NOT do real text search" and returns trending events (e.g. `epl-which-clubs-get-relegated`);
  - concurrency 10 over 697 requests → 429;
  - `m.get("slug")` is the bucket's market slug, not the event slug, so passes 1–2 "added nothing except London" (11 outcomes via slug probing).
- **Decision:** Gamma `/markets` + CLOB scan, event-slug extraction, slug probing at concurrency 3, weather-tag fallback. Slug pattern: `highest-temperature-in-<city>-on-<month>-<day>[-<year>]`.

#### `sd-lowest-markets-off` — "lowest temperature" markets not ingested
2026-05-14 · PR #24 · **adopted-in-code** (`SKIP_MARKET_KEYWORDS`)
- **Q:** Follows nonsense alerts (e.g. "83°F or below YES 99%, median 90°F") caused by inverted old bucket bounds. The reason for dropping lowest markets specifically is not stated.

#### `sd-inclusive-bucket-bounds` — "80–81°F" means [80.0, 82.0)
2026-05-22 · PR #30 · **adopted-in-code** (now `temp_in_bucket`)
- **Result:** Recorded WIN incorrectly, now LOSS: Austin 80–81°F NO with actual 81.0; NYC 68–69°F NO with 69.1; Houston 82–83°F NO with 82.9.

#### `sd-celsius-native-bounds` — store buckets in their own unit
2026-05-26/27 · PR #23 → #35, #37, #38 · **adopted-in-code**
- **Result:**
  - London May 25: 32°C NO @ 68¢ was marked LOSS although the high was 91.4°F = 33.0°C. The rounded-F ranges [90, 92) and [91, 94) overlapped at [91, 92).
  - "29°C or higher" alerts showed P(YES) ≈ 97% at 1–6¢, displayed as "≥29°F", and triggered spurious virtual buys.
- **Decision:** `bucket_unit` column; exact F float interval (e.g. [89.6, 91.4)); `resolve_bucket_unit` falls back to the label. The warmth check in `confidence_scorer` (≥ 66) converts C → F first.

#### `sd-float-dust` — exact-degree boundary flipped
2026-06-02 · PR #51 · **adopted-in-code**
- **Result:** (71.6 − 32) × 5/9 = 21.999999999999996. Paris 22°C NO and Tokyo 27°C NO were recorded WIN although the high was exactly 22.0°C / 27.0°C.
- **Decision:** Round to 4 decimals before comparing. Historical rows were not corrected (to protect P&L).

#### `sd-polymarket-only-resolution` — WIN/LOSS only from Polymarket
2026-06-02 → 2026-06-05 · PR #52, #54, #55, #56, #57 · **adopted-in-code**
- **Result: METAR vs Polymarket winner disagreements:**

  | case | METAR | METAR-math bucket | Polymarket winner |
  |---|---|---|---|
  | Dallas | 93.9°F | 92-93 | 94 |
  | Miami | 91.9°F | 90-91 | 92-93 |
  | Austin Jun 1 | 91.9°F | 90-91 | 92+ |
  | Seoul Jun 1 | 28°C | 27+ | 25 |
  | Houston Jun 03 | 86.0°F | — | 84-85 |
  | Seoul Jun 03 | 31.0°C | — | 27 |
  | NY Jun 04 | 84.9°F | — | 86-87 |

- **Parser bugs found:**
  - `outcomePrices` is a JSON string, so `[0]` read "[" (#52);
  - `outcomes` can be ["No","Yes"], so index 0 is not always Yes (#54: Seoul 27°C+, Hong Kong 33°C+, Austin 90-91 NO wrongly LOSS);
  - `/events?slug=` omits closed events, so all 358 retro checks said "not yet on Poly" (#55).
- **Decision:** METAR may never decide WIN/LOSS; it is shown as "reference only" (#57). Unsettled markets are retried. `MarketOutcome.won`. `resolution_value` = the winning bucket(s).

#### `sd-partial-resolution` — losing buckets close before the winner
2026-06-09 · PR #62 · **adopted-in-code**
- **Result:** A market was marked resolved with an empty winner list, so every YES was recorded LOSS.
- **Decision:** `resolved=True` only when a winner is found and mapped. `_yes_index` no longer guesses index 0.

#### `sd-metar-spike` — a single bad METAR reading
2026-06-13 · PR #72; code `app/analyzers/signal_aggregator.py:209` · **adopted-in-code** (`METAR_SPIKE_THRESHOLD_F = 5.0`)
- **Result:** Hong Kong: METAR showed 89.6°F/32°C while HKO/WU showed 84.7°F/29.3°C, and the alert said `LOCKED — YES IMPOSSIBLE` on a live bucket. Code: "bad 32°C reading vs actual 29.3°C".
- **Decision:** Discard the top reading when it exceeds the 2nd-highest by > 5°F. Intraday: METAR > WU + 4°F → use WU.

#### `sd-collector-retry` — retry and backoff
2026-06-26 · PR #84 · **adopted-in-code** (diagnosis **superseded-by:#88**)
- **Q:** ICON and Tomorrow.io showed `no_data` in all 48 cities since June 19. #84 put this down to transient errors without retry; #88 found the real cause.
- **Decision:** 3 retries at 2/4/8 s on RequestError, 429 and 5xx. No retry on 401/403/404. Response bodies logged.

#### `sd-icon-tio-hk-root-causes` — three data-pipeline holes
2026-07-11 · PR #88 (same body in #87, dated 2026-06-26) · **adopted-in-code**
- **Result:**
  - ICON 100% failure in all 48 cities "since forever": `job_fetch_icon` was never imported or scheduled in `main.py`.
  - Tomorrow.io blocked in all cities but one: a burst of 144 calls against a free quota of 25/hour.
  - Hong Kong had no model at all: added from the UI without coordinates, so every location-based collector skipped it.
  - Earlier, #48: Tomorrow.io and Meteosource were missing from the standalone `scheduler.py`.
- **Decision:** ICON scheduled hourly. Tomorrow.io gets its own job with a budget of 20 calls/hour (480/day), open-market cities first. Hong Kong gets HKO coordinates 22.302, 114.174 (migration 018, only if NULL).

#### `sd-station-audit` — check stations against Polymarket's rules
2026-09-24 · PR #113, #114 · **adopted-in-code** (audit; changes nothing unless "Apply")
- **Q:** If METAR or WU points at a different station from Polymarket's, locks are confirmed on the wrong thermometer: "the likely story behind the losses rounding can't explain (Seoul read 31°C and 26°C won)".
- **Result:**
  - #113's audit said 47 of 48 cities mismatch (45 "resolves on www.weather.gov"). #114 called that a parser error, but #129 later says the rules really had changed.
  - Field problems: Lucknow primary_icao was the literal placeholder `ICAO`, so METAR had never been fetched. Seoul: METAR RKSS (Gimpo) vs WU RKSI (Incheon). Istanbul, Tel Aviv and Hong Kong WU URLs name no station.
- **Decision:** Every verdict carries its rule text and source. "Apply" per city moves the old primary to `reference_icao`. Placeholder ICAOs are flagged.

#### `sd-open-meteo-budget` — batched Open-Meteo under a daily budget
2026-09-24 · PR #115 · **adopted-in-code** (`open_meteo_daily_budget = 8000`)
- **Result:** The legacy jobs sent ~41,000 requests a day for 48 cities against a free tier of 10,000. Projected batched use: ~6,600 weighted requests/day.
- **Decision:**
  - one request per (city, model); core models hourly, ensembles every 6 h;
  - a 429 pauses fetching;
  - record-only extra models (UKMO, JMA, GEM, Météo-France, ECMWF AIFS, NBM US) stored as `om_<model>`, never read by estimators, compared at `/admin/models/compare`;
  - rollback by setting `model_fetch_mode = legacy`.

#### `sd-2025-markets-as-2026` — last year's markets stored as this year's
2026-09-28 · PR #123, #124 · **adopted-in-code**
- **Result:**
  - New York and London had two markets every day: 26 city-days in 10 days.
  - Year-less 2025 slugs were given the current year. They were never priced and were resolved with 2025's winner.
  - The shadow study's NY 26 Sep "83-84°F won, model gave it 4%" was the 2025 result. The real 2026 market resolved 62-63°F with the model at about 61°F.
  - `model_skill` for both cities was polluted. No trade was made on them.
- **Decision:** Skip an event if it is closed or its `endDate` is > 3 days from the parsed date. Migration 028 moves stored ones back when > 180 days apart; nothing deleted.

#### `sd-polymarket-moved-to-nws` — resolution source moved from WU to weather.gov
2026-10-04 · PR #129 · **adopted-in-code** (the audit reads weather.gov links)
- **Result:** "Polymarket moved its temperature markets' resolution source from Wunderground to the NWS observation time series" (e.g. `weather.gov/wrh/timeseries?site=LFPB...`). For the 45 cities: "The rules had changed; the audit wasn't misreading them."
- **Decision:** Read the ICAO from `site=`. Applying a fix rebuilds the Wunderground URL for the resolution station, because the intraday running max still scrapes WU.

#### `sd-nws-obs-21pct` — hourly METAR max misses the settled bucket on 21% of US days
2026-11-16 · PR #133; code `app/nwsobs/__init__.py` · **open/unproven** (record-only)
- **Data:** US market-days "since the stations were fixed on 24 Oct".
- **Result:**
  - the hourly METAR max missed the settled bucket on 21% of US market-days; on 13% the bucket settled higher;
  - the hypothesis that "the five-minute readings are whole °C" was **rejected**: rounding the hourly max to whole °C matches 60% vs 79% unrounded;
  - code: a new max can appear on the 5-min series "up to ~50 minutes before the next METAR".
- **Decision:** Record `api.weather.gov` observations every 10 min for K-stations only (LFPB, RKSI and EGLC return nothing). ~70 requests/hour, ~4,000 rows/day, 60-day retention.

#### `sd-settle-hourly-local-day` — results settle late
2026-11-22 · PR #142 · **adopted-in-code** (`resolution_check_interval = 3600`)
- **Result:** On 23.11, all six 22.11 markets had been settled on Polymarket 7–25 hours earlier, between 15:21 UTC on the 22nd (Seoul, Tokyo) and 09:15 UTC on the 23rd (San Francisco), but still showed "Open". The job ran every 24 h, timed from the last deploy, and used the UTC date.
- **Decision:** Hourly runs. Check a market once its city's local day is over. Model-skill refresh held to once per 20 h. Stragglers (> 3 days) only on the 00 UTC run.

#### `sd-no-orderbook-404` — dead buckets flooded the log
2026-11-26 · PR #143 · **adopted-in-code**
- **Result:** Dozens of tokens were asked every 5 min, each giving two ERROR lines. None belonged to any open temperature event on Gamma, or any closed since 2.11.
- **Decision:** A 404 from `/midpoint` rests the bucket for 1 h with one WARNING naming city, date and bucket.

---

## intraday

#### `id-design` — intraday model and premises
2026-06-10 · PR #65 · **adopted-in-code** (parameters later changed; the "market lags" premise is contradicted by #113 and #143)
- **Model:** final max = max(M, X). M = the METAR running max. X ~ N(μ, σ_h). Locks: `yes_impossible` (bucket below the running max) and `yes_locked`.
- **σ:** shrinks from 2.2°F (6+ h before the end of the peak window) to 0.3°F after the peak. Peak passed = temperature down ≥ 1.5°F for ≥ 90 min after 14:00.
- **Premise (no data):** "The market lags the METAR, still pricing 5–15¢ of 'maybe' after the max is locked. That's our profit."
- **Settings:** blend weights HRRR 2.0, NWS 1.5, GFS/ECMWF 1.25, others 1.0. Start 10:00 local, peak window 14:00–17:00, alert 90% / buy 94%, min edge 5¢.

#### `id-bias-and-local-day` — missing bias, UTC dedup, silent settlement
2026-06-11 · PR #67 · **adopted-in-code**
- **Result:** The intraday blend lacked the +1.5°F bias, so the expected max was systematically ~1.5°F low. Dedup used UTC midnight (16:00–17:00 in US cities), allowing a double record and buy on the same local day. Intraday positions settled with no message.

#### `id-wu-official-running-max` — the Denver incident
2026-06-11 · PR #68 · **adopted-in-code**
- **Result:** Denver: WU's official page showed 74°F while the bot said "running max 73.0°F → expected final 73.0°F".
- **Decision:**
  - running max = max(METAR, WU); a WU reading older than 90 min is ignored, and WU > METAR + 4°F is "suspect" and shown only;
  - peak-passed detection stays on the METAR scale;
  - WU removed from the intraday blend, because its same-day "high" is the observed-so-far max.

#### `id-tokyo-sigma-floor-preyes-cap` — Tokyo 96% buy lost on a bucket floor
2026-06-12 · PR #69; code `app/intraday/estimator.py:59-67` · **adopted-in-code** (`spread_sigma_weight = 0.5`, `pre_peak_yes_cap = 0.90`)
- **Result:** The running max sat exactly on the bucket floor (78.8°F = 26°C). Models disagreed by Δ4.7°F at 13:10, before the peak window. σ = 1.0 → 96.4%.
- **Decision:** σ = max(schedule, gain_weight × spread × 0.5). Unlocked YES before 14:00 is capped at 90%. The reconstruction now gives ≤ 90%, so no buy.

#### `id-realert-2pp-cooldown` — Guangzhou re-alert noise
2026-06-12 · PR #69; code `app/intraday/detector.py:53-57` · **adopted-in-code** (`REALERT_CONF_DELTA = 0.02`, `REALERT_COOLDOWN_MIN = 15.0`)
- **Result:** 3 updates in 30 min (93 → 94 → 96) from mechanical σ time-decay.
- **Decision:** Re-alert threshold 1pp → 2pp, plus a 15-min cooldown per (outcome, side).

#### `id-paris-forecast-sigma-cap96` — Paris 98% NO lost
2026-06-12 · PR #70; code `app/intraday/estimator.py:68-80` · **adopted-in-code** (`same_day_forecast_sigma = 2.5`, `stat_prob_hi = 0.96`)
- **Result:** Running max 66.2°F, expected final 73.8°F (almost all forecast), σ from the schedule only (±1.6°F) → 98% NO. The day beat every model and the "impossible" bucket won.
- **Decision:** σ = √(σ_schedule² + (w·2.5)²). Statistical cap 96% for anything not a lock and not post-peak; locks keep 98.5%. The reconstruction gives 98% → ~93%, below the 94% buy.

#### `id-wu-confirmed-lock-celsius-floor-cluster` — four CSV-driven intraday fixes
2026-06-13 · PR #74; code `app/intraday/estimator.py:48-54` · **adopted-in-code**
- **Data:** "June 2026 intraday positions CSV" (n not stated).
- **Result:** "METAR runs 2-4°F above WU, the resolution source". Post-peak σ = 0.3°F "manufactured >99% confidence on a single-decimal flip" in 1°C (= 1.8°F) buckets.
- **Decision:**
  - `yes_impossible` → `yes_impossible_unconfirmed` when the max is METAR-only;
  - Celsius σ floor 1.8°F;
  - max entry 88¢ (see pricing);
  - cluster early warning: a city running > 2°F above forecast boosts sister-city bias.

#### `id-peak-confirm-taipei` — false "peak passed" at 14:14
2026-06-19 · PR #75; code `app/intraday/estimator.py:155-163` · **adopted-in-code** (`peak_confirm_hour = 15.5`)
- **Result:** Taipei 14/6: "peak passed" was declared at 14:14 on a transient 1.8°F dip. σ collapsed to 0.3, certainty reached 96%, the temperature climbed again → loss. "The daily max typically lands 14:00-16:00."
- **Decision:** Cooling-based peak detection only after 15.5 h local.

#### `id-cross-bet` — holding both sides of one bucket
2026-06-19 · PR #75; code `app/intraday/detector.py:345` · **adopted-in-code**
- **Result:** London 15/6: 21°C NO @ 60¢ + 21°C YES @ 74¢ = $1.34 paid to get $1.00 back.
- **Decision:** Block the buy when an open opposite-side position exists; the alert is kept.

#### `id-unconfirmed-lock-99` — the 99% band was the biggest loser
2026-06-23 · PR #81; code `app/intraday/estimator.py:81-88, 347-353` · **adopted-in-code** (`unconfirmed_lock_sigma_f = 2.0`)
- **Data:** intraday positions export, 637 rows, 200 executed.
- **Result:**
  - the 99% band: 72 trades, 78% actual win rate, −$14.53;
  - σ < 1.0°F lost −$16.07; σ ≥ 1.6°F made +$18.96;
  - cause: METAR over the ceiling without WU confirmation, with post-peak σ 0.3 → a 2°F METAR-over-WU gap gave 98.5% NO; WU then resolves 1–3°F lower.
- **Decision:** `bucket_probability` receives `wu_confirmed`. Unconfirmed σ floor 2.0°F: a 2°F excess → ≈ 84% NO; 1°F → ≈ 69%. The 96% cap is extended to unconfirmed locks after peak.

#### `id-intraday-loses` — intraday loses money significantly
2026-09-24 · PR #113 · **observation**
- **Data:** 5,418 settled intraday bets.
- **Result:** Realised edge −1.88pp, t = −3.35.

#### `id-lock-margin` — "impossible" locks need one unit past the edge
2026-09-24 · PR #113; code `app/intraday/estimator.py:89-98` · **adopted-in-code** (`lock_margin_f = 1.0`, `lock_margin_c_f = 1.8`)
- **Result:**
  - WU-confirmed `yes_impossible` NO bets won only 77.5% (89 bets). The market priced them near 80¢: "the market was right."
  - 58 of 67 were declared with the max ≤ 0.25 unit past the bucket top.
  - METAR reports whole °C, so "90.0°F" can be anything from 88.7 to 90.5.
  - `yes_locked`: only 2 in the data (both won), so that half "rests on the measurement argument, not on evidence".
- **Decision:** Below the margin the state is `yes_impossible_marginal`, priced statistically like `unconfirmed` (`SOFT_LOCK_STATES`).

#### `id-lock-state-overflow` — Postgres-only silent cycle loss
2026-09-24 · PR #113 · **adopted-in-code**
- **Result:** `lock_state` was VARCHAR(20) but `"yes_impossible_unconfirmed"` is 26 characters. On Postgres the failed flush made every later insert in the session fail, so the rest of the intraday cycle was lost silently. SQLite ignores the length, so no test saw it.
- **Decision:** VARCHAR(32), plus a test that every state fits.

---

## nearby-stations

#### `nb-stage1-logloss` — neighbours improve "will the max still rise"
2026-11-16 · PR #134; code `app/nearby/__init__.py` · **open/unproven** (collection only)
- **Data:** IEM archive Jul–Nov, 35 cities. Trained Jul–Sep, tested Oct–Nov.
- **Result:**
  - log-loss improves 8.5% (95% CI excludes zero), in every hour 11:00–16:00; AUC 0.945 → 0.955;
  - a "buy only when the max is locked" rule against Oct–Nov bid/ask made +1.4 to +1.7 c/share with neighbours, positive in both halves; without neighbours it lost money;
  - "not significant (t ≈ 1)".
- **Decision:** Staged plan (collect → paper → dashboard). Up to 5 active METAR stations 3–80 km from each of 32 settlement stations, 120 in all, keyed by settlement station. One batched request per run, only for cities with an open market inside 10:00–20:00 local.

#### `nb-rule-backtest-and-decay` — the nearby rule and its speed decay
2026-11-17 · PR #135; code `app/nearby/signal.py:1-17` · **open/unproven** (paper only)
- **Data:** Jul–Nov: Polymarket minute price history for 4,828 events, IEM METARs for the settlement station plus up to 6 nearby stations in 35 cities. Rule chosen on Jul–Aug only.
- **Rule:** Buy the bucket holding the settlement station's high so far when ≥ 60% of nearby stations are already ≥ 1°F below their own high and the mid is 30–85¢.
- **Result, net of a modelled spread:** Jul–Aug +6.5¢, Sep +4.0¢, Oct–Nov +4.5¢ per share.
  - Positive every month. Leave-one-city-out: +2.8¢ to +5.5¢. Settlement station alone: −0.6¢ to +0.9¢.
  - **Decay:** +5.4¢ when bought within 5 min, +3.0¢ at 10, +0.9¢ at 30, negative at 45+.
  - Real recorded spreads at these moments ~7¢ (median). The direct check at own recorded asks (n = 87, 0–20 min after the signal) was **−4.7¢**. OOS t = 1.7–1.85.
  - Bucket membership by rounding matched settlement on 98.7% of 1,309 US station-days; truncating did not match as well.
- **Decision:** Live paper test: first in-band signal per market reads the book, records bid/ask/size, sends one Telegram message, records the mid +10/+30 min. Nothing bought. Collection 10 → 5 min "because the signal decays within minutes".

#### `nb-tradable-ask-spread` — real ask required
2026-11-17 · PR #136 · **superseded-by:#139**
- **Result:** Munich, first day live: the mid of 84¢ was in band but the book was bid 69.2¢ / ask 99.5¢ (30¢ spread).
- **Decision:** A paper buy counts only if ask ≤ 88¢ (band top + ~3¢) and spread ≤ 10¢. Untradable signals are recorded but neither announced nor scored.

#### `nb-first-sighting-lock` — an untradable first book silenced the market
2026-11-17 · PR #137 · **adopted-in-code**
- **Result:** No alert went out after #136. A market's first, wide-book signal closed it for the day. "Spreads at signal moments are often wide (~7¢ median)."
- **Decision:** Only a tradable signal closes a market. `first_seen_at` records the wait.

#### `nb-after-peak-fair-value` — fire only after the usual peak; buy below fair value
2026-11-20 · PR #139; code `app/nearby/signal.py:28-44` · **adopted-in-code** (paper; `PEAK_SHARE_MIN = 0.5`, `FAIR_BY_MID`, `MARGIN = 0.02`)
- **Data:** the first ten live signals, checked against full-day IEM METARs and Gamma; plus Jul–Nov history, with the typical peak learned on Jul–Aug.
- **Result, live:**
  - SF 17.11 14:00: max 59°F at the signal, then 60°F at 14:56, settled 60–61 → lost (fired before the peak);
  - LA 19.11 11:30: 64°F, then 66°F at 12:53 → "will lose";
  - Madrid 17.11 19:00: 16°C until 17°C at 21:00 (rare warming at night) → lost;
  - the other 7 won, but at asks of 92–99.5¢, "with nothing left to win".
- **Result, history:**

  | | Jul–Aug | Sep | Oct–Nov | win | OOS t |
  |---|---|---|---|---|---|
  | as live | +6.5¢ | +4.0¢ | +4.5¢ | ~71% | 1.85 |
  | + after usual peak | +7.4¢ | +8.3¢ | +7.0¢ | ~79% | 2.96 |

  - Fair value 54/67/74/85/92¢ for mids up to 50/60/70/80/85¢ (code: 0.538/0.667/0.735/0.850/0.915); Sep–Nov matched within a few points.
  - Buying only when the ask is ≥ 2¢ below fair: OOS +8.4¢/share, t = 3.24, ~3.5 signals/day.
  - Above 90¢ history is flat (win 95–96%, net ≈ 0).
- **Decision:** The peak condition plus fair-value buyability replace the flat 10¢ spread cap ("a 16¢ spread is a buy at mid 75¢ with an 83¢ ask; a 99.5¢ ask never is").

#### `nb-city-differences-not-real` — no city switched off yet
2026-11-20 · PR #140 · **adopted-in-code** (display; no city gating)
- **Result:** A city's Jul–Sep result did not predict its Oct–Nov result (rank correlation 0.0). Cities positive in Jul–Sep made +6.0¢ in Oct–Nov; negative ones made +9.2¢.
- **Decision:** Show per-city records, marked "enough data" at ~30 settled buyable signals. Per-station history is in `NEARBY_HISTORY` (113 of 120 stations), as research context only.

#### `nb-station-history-table` — per-neighbour measured lift (code data)
code · `app/nearby/stations.py:207-212` · **open/unproven** (research context, not traded)
- **Data:** Jul–Nov IEM METARs. After the usual peak, when the neighbour is ≥ 1°F below its own high: how many pp more often the settlement station was already at the day's high. Reported as (Jul–Sep, Oct–Nov).
- **Result:** e.g. (CYYZ, CXTO) 7.1 / 9.2; (EDDM, ETSI) 7.3 / −3.5; (EHAM, EHLE) 7.4 / −1.5. **(derived)** Across 113 pairs: Jul–Sep ranges −3.1 to 28.6pp, Oct–Nov −14.7 to 20.3pp, 11 pairs negative in Oct–Nov.

#### `nb-asia-market-knew-first` — the signal fires, but the price is already above 85¢
2026-11-26 · PR #143 · **observation**
- **Data:** a replay of 26.11 with `signal.py` on that day's real METARs vs Polymarket price history.
- **Result:**

  | city | condition first met (local) | bucket | price |
  |---|---|---|---|
  | Tokyo | 15:00 | 15°C | 95¢ → 99.5¢ |
  | Seoul | 17:00 | 15°C | 99.9¢ |
  | Singapore | 18:00 | 32°C | 99.9¢ |
  | Kuala Lumpur | 18:00 | 32°C | 99.7¢ |

  "The market knew first." Europe at 12:50 UTC: 8 cities checked, none fired.
- **Decision:** Per-city status counters (above band, below band, before peak) so a quiet day can be told apart from a stuck job.

---

## peaks

#### `pk-solar-noon-variation` — one fixed peak window does not fit all cities
2026-09-27 · PR #121 · **open/unproven** (record-only `daily_peaks`)
- **Data:** 2,760 settled intraday alerts, June–July only.
- **Result:** Summer solar noon spans 11:41 in Tokyo to 14:14 in Madrid. Between 14:00 and 16:00, late-solar-noon cities won 71.7% vs 77.4% for early-sun cities: "not significant (z≈1.2)".
- **Decision:** Record each city-day's high and when it came. Months with < 10 complete days are shown as an estimate (NOAA solar noon + the measured offset; default offset 2.5 h in code, `peaks/report.py:21`). Same spike rule as the aggregator.

#### `pk-peak-guard` — entering before the city's usual peak loses
2026-10-03 · PR #127; code `app/peaks/guard.py:1-8`, `app/intraday/detector.py:712-717` · **open/unproven** (shipped OFF: `intraday_peak_guard_enabled = False`; recorded on every signal)
- **Data:** 500 settled intraday trades, June–July, all entered at 14:00 local or later.
- **Result:** Entered when fewer than half of that city's days in that month had already peaked by that hour → won 65%; otherwise 78%. z = 3.0; holds in both halves of a time split and within cities (z = −2.8). Stated confidence was 94% vs 95%, so the bot did not see the difference.
- **Decision:** Record `_peak_guard_would_block` and block the buy only if enabled (threshold 0.5). Fewer than 10 measured days or a read error → None, which never blocks. Reported at `/admin/intraday/peak-guard`. No result from the recorded guard is reported in later PRs. The same idea was adopted for nearby (#139).

---

## timing/lead-time

#### `tl-scan-7-days` — markets exist ~7 days ahead
2026-05-13 · PR #12 · **adopted-in-code**
- **Result:** "Polymarket creates markets for each of the ~7 next days."

#### `tl-lead-sigma-schedule` — σ by lead time
2026-05-20 · PR #28 · **superseded-by:#46, #73**
- **Result:** With a flat σ = 3°F a perfectly centred forecast gives ~26% on a 2°F bucket. SF recomputed with σ = 2.0: NWS 73°F → P(72–73°F) ≈ 38%.
- **Decision:** σ = 1.5/2.0/2.5/3.0/3.5/4.5°F for leads 0/1/2/3/4/5+.

#### `tl-observations-same-day-only` — today's obs do not predict tomorrow
2026-05-20 · PR #28 · **adopted-in-code**
- **Result:** SF alert for May 21 (1 day out) showed "Reference wind: +1.1pp", taken from today's KSFO METAR. Dallas coordinates 32.900°N, 97.040°W were the airport, ~25 mi NW of the centre (see Conflicts).
- **Decision:** No METAR-trend, reference-wind or PIREP adjustments when days_ahead ≥ 1.

#### `tl-horizon-and-daily-dedup` — horizon 3 days; re-evaluate multi-day markets daily
2026-05-22 · PR #30 · **adopted-in-code** (`max_days_ahead_for_alert = 3`)
- **Decision:** Markets more than 3 days out are not analysed. Dedup per calendar day instead of forever, "useful when you want to enter/exit as the forecast shifts".

#### `tl-close-in-city-timezone` — hours-to-close in local time
2026-05-23 · PR #31 · **adopted-in-code**
- **Result:** Denver markets showed "~4h" when alerted at 07:00 Israel time instead of ~24 h.

#### `tl-local-day-windows` — UTC day windows contaminate the running max
2026-06-09 · PR #62; code `signal_aggregator.py:203-206` · **adopted-in-code**
- **Result:** "A UTC day window includes the previous local evening for US cities, contaminating the running max with yesterday's late-afternoon warmth."

#### `tl-model-update-times` — when models actually publish
2026-09-24 · PR #116 · **open/unproven** (measurement tool)
- **Q:** HRRR publishes hourly but was fetched hourly (data up to ~60 min old). Fixed 6-hour UTC fetches might sit just before or after new runs.
- **Decision:** HRRR also at :50 (~290 more requests/day). Change detection into `model_update_events` (~100 rows/day). An hourly probe of one city per slow model (~320 requests/day). Projected ~7,300 weighted requests/day, under the 8,000 cap. No result reported.

#### `tl-same-day-91-real-track` — only same-day ≥91% made money in every quarter
2026-11-21 · PR #141; code `app/realtrack/__init__.py`, `track.py:90` · **open/unproven** (virtual buys in their own table; the owner buys by hand)
- **Data:** May–November positions export, 3,874 buys, by quarter.
- **Result:**

  | slice | buys | win | paid | edge at the ask |
  |---|---|---|---|---|
  | same day, ≥91% | 928 | 76.4% | 73.4¢ | +3.0¢ (t=2.39, 4/4 quarters) |
  | same day, ≤90% | 331 | 63.7% | 68.6¢ | −4.8¢ |
  | one day ahead, ≥91% | 896 | 71.4% | 73.7¢ | −2.3¢ (0/4 quarters) |

  In 1,540 of 2,583 market-days the first research buy was made before the event day, so the research dedup hid the same-day entry.
- **Decision:** An in-memory hook records same-day (city calendar), ≥ 91% signals as 5-share virtual buys at the live price with their own alert. `max_days_ahead_for_alert` was not set to 0, because it also drives price polling and research buys.
- **Known limits:** the 30-min market cooldown delays entries; UTC-date scanning stops US "today" markets after UTC midnight (~18:00–20:00 local).

---

## cities

#### `ct-blacklist` — alert but never fund
2026-06-02 · PR #51 · **adopted-in-code**
- **Decision:** `cities.blacklisted`: alerts still fire ("so we keep tracking and learning") but no virtual buy is opened (example: Paris).

#### `ct-onshore-wind-per-city` — the sea-breeze heuristic was West-Coast-only
2026-06-09 · PR #62 · **adopted-in-code**
- **Result:** The fixed 270–340° onshore bearing was right only for the US west coast, and the sea-breeze logic was inverted at the 66°F boundary.
- **Decision:** `City.onshore_wind_dir` (e.g. LA ≈ 300, Miami ≈ 110). Empty = heuristic off.

#### `ct-variance-city` — London archetype
2026-06-19 · PR #78; code `beta_estimator.py:53-56, 301` · **adopted-in-code** (beta)
- **Rule:** High MAE (≥ 3.5) with near-zero bias (≤ 1.0) across ≥ 2 sources is a "pure-noise city": beta σ × 1.2.

#### `ct-forward-test-z3` — judge cities only on trades after a chosen date, at z>3
2026-10-03 · PR #128; code `app/research/performance.py:69` · **adopted-in-code** (research tool)
- **Q:** "What makes Paris profitable, and can we apply it to other cities?" Choosing the best cities from results already seen cannot separate skill from luck.
- **Rule:** "With ~40 cities, z=2 appears by chance in about one of them", so a city is called only past z = 3, on trades after a chosen date. Also a models-vs-actual export (lead 0–3) per city-day.

---

## costs/infrastructure

#### `ci-scheduler-in-process` — scheduler crashes were invisible
2026-05-13 · PR #6, #7 · **adopted-in-code**
- **Result:** A missing abstract method crashed `jobs.py` on import, "killing the scheduler silently on every deploy".
- **Decision:** Scheduler runs inside the FastAPI lifespan, so errors appear in the Railway logs. (Side effect: jobs registered only in the standalone `scheduler.py` never ran; see #48 and #88.)

#### `ci-sql-echo` — SQL echo flooded the logs
2026-05-14 · PR #15 · **adopted-in-code**
- **Result:** 500+ lines/sec hit the rate limit and 467 messages were dropped per batch; the latest visible logs were 3 hours old.
- **Decision:** Echo off by default (`SQLALCHEMY_ECHO=1` to enable).

#### `ci-healthcheck-300` — migration exceeded the healthcheck
2026-05-26 · PR #36 · **adopted-in-code**
- **Result:** Migration 005 took over 2 minutes against a 120 s healthcheck.
- **Decision:** `healthcheckTimeout` 300.

#### `ci-forecast-dedup` — forecasts duplicated hourly forever
2026-07-22 · PR #90 · **adopted-in-code** (dedup on; hard prune off and locked off by test #98)
- **Result:**
  - $32/month, ~78% RAM, climbing from 1.78 GB to 3.55 GB;
  - `forecasts` ~1.9M rows/month, "95%+ of them hourly duplicates";
  - dedup claimed to be ~90% of the saving;
  - expected ~$32 → ~$13–16/month (prediction).
- **Decision:** Keep one row per (city, source, target date, made-day): "exactly what model_skill and the aggregator read". DB pool 30 → 7. Prune off until backups exist.

#### `ci-vacuum` — deleted rows do not free disk until VACUUM
2026-07-22 · PR #91 · **adopted-in-code**
- **Decision:** A plain VACUUM (ANALYZE) daily on changed tables; a one-time VACUUM FULL on demand.

#### `ci-gc-heartbeat-freeze` — a diagnostic froze the bot every 15 min
2026-07-25 · PR #93, #94 · **adopted-in-code** (light path only)
- **Q:** The RAM curve kept climbing after the DB fixes (#93 calls it "linear") → is the app leaking?
- **Result (measured):**

  | | 800K objects | leaking process (5–10M) |
  |---|---|---|
  | full heap scan | 106 ms | ~0.7–1.3 s |
  | + `gc.collect()` | 128 ms | ~0.8–1.6 s |
  | total | ~300 ms | ~2-3 s |

  It holds the GIL: ~96 freezes/day. The light path costs 0.039 ms ("~25,000× cheaper").
- **Decision:** #93 merged the dangerous version and #94 hotfixed it. The heartbeat uses RSS plus `sys.getallocatedblocks` only; the deep scan is opt-in and never scheduled.

#### `ci-vacuum-full-skipped-big-table` — "Shrink" never touched market_prices
2026-07-25 · PR #95 (dup #96) · **superseded-by:#105, #107** (VACUUM FULL turned out to be the wrong lever)
- **Result:** market_prices, 2,963 MB of 3,871 MB, was never in the list; errors were swallowed (`tables_vacuumed: 0`). Postgres row estimates showed 38,800 → 0 → 5 in three minutes.
- **Decision:** All tables, smallest first; errors returned. An exact-COUNT button (later reverted, see `ci-count-star-cost`).

#### `ci-indexes-and-write-on-change` — the real cost drivers
2026-07-25 · PR #97 · **adopted-in-code**
- **Result:**
  - `opportunities` had no index: a ~55 MB seq scan plus sort (12,800 rows) on every screen load;
  - a price row was written per outcome every 5 min even when unchanged, with a commit per outcome (~1,000 transactions/run): ~290,000 rows/day, "17.5 million rows, ~3 GB";
  - ~650 MB of duplicate indexes (UniqueConstraint + Index on the same columns);
  - exact counts: market_prices 17,532,604 rows at 177 B/row; forecasts 1,040,351 at 683 B/row;
  - the Postgres estimate said 3,561 rows, an error of ~4,900×, so the earlier "BLOATED" diagnosis was wrong.
- **Decision:**
  - migration 021 indexes, including a partial index on positions;
  - write a price only on change, with a 1-hour heartbeat and one commit per run;
  - migration 020 drops the duplicate indexes;
  - expected growth ~290,000 → ~30,000 rows/day (−90%).

#### `ci-price-trend-unused` — 230,000 daily queries nobody read
2026-07-25 · PR #98 · **adopted-in-code**
- **Result:** `_price_trend` ran a 60-min range scan on the 17.5M-row table per outcome per analyzer run: "~800 × 288 = 230,000 queries a day", and no code read the result. Audit of `market_prices` readers: last row, last 60 min, last 24 h, latest price; nothing older than 24 h.
- **Decision:** Removed (key kept as None). Tests fail if it gains a consumer, or if prune or dedup ever touch market_prices.

#### `ci-httpx-logs` — 460,000 noise lines/day
2026-07-26 · PR #99 · **adopted-in-code**
- **Result:** ~1,600 calls per 5-min cycle → ~460,000 lines ≈ 70 MB/day.
- **Decision:** httpx, httpcore, urllib3, telegram.ext and apscheduler.executors logged at WARNING.

#### `ci-count-star-cost` — the diagnostic button cost money; write-on-change confirmed
2026-07-27 · PR #100 (also #101) · **adopted-in-code** (ANALYZE + reltuples)
- **Result:**
  - COUNT(*) read ~3 GB per click; the cost rate rose from $0.27/day to $1.52/day during the investigation window, partly the diagnostics' own cost;
  - measured over 20.2 h on the deployed build: total DB ~68 → 13.2 MB/day (−81%); market_prices ~52 → 9.0 MB/day (−83%); forecasts ~38 MB/day → +0.1 MB in 20 h (~−99%);
  - headroom until the volume is full: ~10 days → ~76 days;
  - plus 742 MB freed by dropping duplicate indexes.

#### `ci-model-skill-toast` — an hourly job pulled 692 MB of TOAST
2026-07-27 · PR #101 · **adopted-in-code**
- **Result:** Postgres = 97% of the bill ($23.71 vs $0.68 for the app), RAM dominant. `update_model_skill` ran hourly for 48 cities with `select(Forecast)` over 90 days, including `raw_data`, dragging most of the 692 MB through the page cache every hour.
- **Decision:** Select only the 4 columns it reads; results identical.

#### `ci-tiered-price-polling` — stop polling markets beyond the horizon every 5 min
2026-07-28 · PR #102 · **adopted-in-code**
- **Result:** After RAM fell from 4.03 to 1.59 GB, the cost was ~$0.82/day: RAM ~65%, CPU ~18%, egress ~6% (13 GB, growing ~1 GB/day).
- **Decision:** Markets ≤ 3 days ahead every 5 min (unchanged); beyond that every 30 min; no date or no history → always.

#### `ci-raw-data-strip` — the 3.9 GB is live data, not bloat
2026-09-22 · PR #105, #109; code `retention_job.py:208-213`, `config.py:210-216` · **adopted-in-code**
- **Result:** Total 3,892.3 MB; dead rows 5,611 out of 21.5 million ("essentially zero bloat").

  | table | total MB | index MB | rows | B/row |
  |---|---|---|---|---|
  | market_prices | 2,754.3 | 1,348 (49%) | 21,477,844 | ~134 |
  | forecasts | 696.0 | 36.8 | 267,471 | ~2,728 |
  | pireps | 129.2 | 14.8 | 689,178 | ~197 |
  | opportunities | 115.1 | 2.0 | 27,184 | ~4,439 |

  `raw_data` ≈ 640 MB, "a sixth of the entire database"; its only reader asks for today or later. Expected volume ~85% → ~72%. Volume costs $0.15/GB-month, "a few cents". #109 verified on PG16: 212 eligible, 212 cleared, rows unchanged; production's 0 was a genuine no-match.
- **Decision:** Clear `raw_data` 7 days after the target date, in batches of 2,000 (WAL spike risk); no rows deleted. Per-index diagnostics.

#### `ci-csv-streaming` — exports were not streaming
2026-09-22 · PR #106 · **adopted-in-code**
- **Result:** 8,000 rows × 4.4 KB = a 34 MB file: peak 103.5 MB buffered vs 0.6 MB streamed. Memory was 67% of the bill ($9.47 of $14.13). The RAM rise was "step-shaped, not a smooth ramp".
- **Decision:** Server-side cursor, 500-row batches, 64 KB chunks. A mid-stream failure writes `# EXPORT FAILED`. Postgres page cache is still unbounded without a service memory limit.

#### `ci-vacuum-full-cap` — never rewrite a table bigger than the free disk
2026-09-22 · PR #107; code `retention_job.py:65-71` · **adopted-in-code** (`vacuum_full_max_table_mb = 1000`)
- **Result:** market_prices 2,754 MB vs ~750 MB free: the rewrite would fill the volume to 100%, and "while the disk is full, the bot's own INSERTs fail too".
- **Decision:** Skip and report anything larger than the cap. Plain VACUUM is never capped. Forecasts 696 → ~56 MB after stripping.

#### `ci-cpu-per-job-and-sessions` — CPU accounting; sessions survive deploys
2026-09-23 · PR #110 · **adopted-in-code**
- **Result:** CPU = 38% of the bill ($4.09 of $14.80) at ~0.39 vCPU. Admin sessions were an in-memory set, so every deploy caused 401s (DB size worked at 20:52; #108 deployed; Shrink failed at 21:30). The example job table (polymarket 49.0% CPU, analyzer 38.1%) may be illustrative.
- **Decision:** Wall and CPU time per job; unattributed CPU shown. HMAC-signed tokens.

#### `ci-dependency-pins` — an unpinned rebuild broke the deploy
2026-09-26 · PR #119 · **adopted-in-code**
- **Result:** `sqlalchemy>=2.0.25` resolved to 2.1.1, where greenlet moved into the `[asyncio]` extra → ImportError at `alembic upgrade`.
- **Decision:** `sqlalchemy[asyncio]>=2.0.25,<2.1` and `apscheduler>=3.10.4,<4` (4.x removes `AsyncIOScheduler`).

#### `ci-shadow-export-size` — exports outgrew a phone upload
2026-10-03 → 2026-11-05 · PR #126, #130, #131 · **adopted-in-code**
- **Result:** Shadow CSV 47 MB; ZIP "about 10x smaller"; > 30 MB zipped by 5 Nov. On 23 Oct the export failed with `InFailedSQLTransactionError` (retry inside an aborted transaction).
- **Decision:** ZIP streaming; roll back before the fallback; read one day at a time (BRIN); `since` date.

---

## risk-guards

#### `rg-one-alert-per-market` — keep only the best-edge bucket
2026-05-14 · PR #23 · **superseded-by:#63** (multi-bucket alerting)
- **Result:** London produced both "11°C YES" and "12°C or higher YES" for the same market.

#### `rg-dedup-evolution` — what counts as a duplicate
2026-05-14 → 2026-06-13 · PR #24, #30, #41, #62, #72 · **adopted-in-code** (current: per (outcome, side) today, OR any open position on (outcome, side))
- #24: never re-send the same (outcome, side).
- #30: per calendar day.
- #41: any side, because Seoul got YES + NO on the same outcome the same day.
- #62 reversed that: "dedup ignored side — NO today blocked YES".
- #72: plus any open position regardless of day (`rg-double-position`).

#### `rg-market-cooldown` — one market, conflicting alerts minutes apart
2026-05-27 · PR #38 (dup #39) · **adopted-in-code** (`alert_dedup_minutes = 30`)
- **Result:** Atlanta: "84-85°F NO @ 86%" at 10:00, then "82-83°F YES @ 75%" at 10:05, because the best-edge pick shifted between 5-min runs.
- **Decision:** Skip a whole market for 30 min after any alert in it. #141 notes that this delays real-track entries.

#### `rg-auto-suspension-streak` — 3 high-confidence losses in a row
2026-06-10 · PR #63 · **adopted-in-code** (`suspension_consecutive_losses = 3`, `suspension_days = 7`)
- **Decision:** Suspend for 7 days after 3 consecutive high-confidence losses. Alerts continue during the suspension.

#### `rg-open-position-realert` — a signal on an already-held position
2026-06-10 · PR #63, #64 · **adopted-in-code** (`OPEN_POS_REALERT_CONF_DELTA = 0.01`)
- **Result:** 🔁 alerts went out every ~5 min.
- **Decision:** Once per day, then only when confidence moves ≥ 1pp. Entry price removed as a trigger ("changes too often").

#### `rg-bucket-switch-yes-only` — the Chicago incident
2026-06-12 · PR #69; code `opportunity_detector.py:790-796` · **adopted-in-code**
- **Result:** The bot suggested abandoning a 95% NO because an adjacent 83% NO appeared. NO positions on different buckets are complementary.
- **Decision:** Switch alerts for YES only. Entry shown from the book (was 0¢).

#### `rg-near-money` — warn on the bucket holding the forecast
2026-06-13 · PR #72, #74 · **adopted-in-code** (warning only)
- **Q:** The modal bucket has the worst risk/reward: a 1–2°F error either way is a loss.
- **Decision:** `⚠️ NEAR-MONEY BUCKET` warning. #74: the detector had read `det_avg` (a probability) as a temperature, so it always fell back to the highest-probability bucket.

#### `rg-double-position` — the same bucket bought on consecutive days
2026-06-13 · PR #72; code `opportunity_detector.py:555-559` · **adopted-in-code**
- **Result:** Seoul/Tokyo: the same bucket was opened on day T and day T+1, both settling as losses. #75: "zero duplicates after 13 Jun".

#### `rg-exit-monitor` — virtual stop-loss signals
2026-06-26 · PR #86, #87 · **adopted-in-code** (audit trail only; no later evaluation reported)
- **Decision:** Exit signal when confidence drops ≥ 20pp AND the forecast moves ≥ 2°F, OR confidence falls below 55%, OR the forecast moves ≥ 5°F. Written to `virtual_exits` for "how much would we have saved". Bugs found in the beta path along the way: the tuple from `beta_blend_with_market` was used as a probability, and the market price was inverted.

#### `rg-chronic-suspension` — a city that alternates W/L never trips the streak rule
2026-07-11 · PR #88; code `opportunity_detector.py:142-146`, `config.py:191-197` · **adopted-in-code** (`suspension_window_trades = 10`, `suspension_min_win_rate = 0.65`)
- **Result:** "Kuala Lumpur archetype": 55% (#88); code: "W-L-W-L-L-W at 56%".
- **Decision:** Suspend when < 65% over the last 10 settled ≥ 90% trades.

#### `rg-persistent-settings` — admin settings reset on every deploy
2026-07-11 · PR #88 · **adopted-in-code**
- **Result:** "Alerts at 7x% despite threshold 80": `min_confidence_alert_near = 0.75` overrode the main threshold, and PATCH changed memory only.
- **Decision:** `app_settings` table with a whitelist. Near default 0.80.

#### `rg-suspension-false-alarms` — the chronic rule fires by chance
2026-09-23 · PR #108; code `config.py:178-186` · **adopted-in-code** (master switch + report; defaults unchanged)
- **Result:** At a realised ~78%, the SE over 10 trades is ~13pp, so 65% sits ~1 SE below expected. Binomial false alarms per check:

  | window | min rate | false alarms per check |
  |---|---|---|
  | 10 | 65% | 15.9% |
  | 20 | 65% | 5.4% |
  | 30 | 65% | 4.8% |
  | 10 | 50% | 1.0% |

  The check re-runs on every settlement, so "almost every city eventually trips it by luck".
- **Decision:** `suspension_enabled` switch; all knobs persisted; windows of 1–4 refused. Recommendation: window = 30, leave the rate (owner to decide). Expiry cleanup runs before the enabled check, so turning it off cannot strand suspended cities. The sample report (Austin 7/10, Chicago 5/10) may be illustrative.

---

## other

#### `ot-pnl-subset-bug` — cost and P&L over different subsets
2026-06-01 · PR #50 · **adopted-in-code**
- **Result:** Cost $238.51 covered all 64 positions while Payout $145 and Net +$4.63 covered only ~38 closed ones; the user expected −$93.
- **Decision:** Show "Cost (closed)" and "At risk (open)" separately.

#### `ot-dashboard-truncation` — a wider date range showed fewer rows
2026-06-07 · PR #58 · **adopted-in-code**
- **Result:** 28 positions at 95%+ for 1 Jun → yesterday, "far fewer" once today was added. A server cap of 200 rows hid them from client-side filters. Stats aggregates were unaffected.
- **Decision:** Cap 5000 with a visible warning; `/opportunities` N+1 query fixed.

#### `ot-review-bug-sweep` — 11 review bugs
2026-06-09 · PR #62 · **adopted-in-code**
- **Bugs with trading impact:**
  - the 92% clip was bypassed after normalization → re-clip;
  - normalization over a partial bucket set inflated probabilities → normalize only when all buckets have data;
  - un-alerted positions never settled;
  - `or fallback` treated a 0.0 threshold as missing;
  - year rollover (a January slug seen in December);
  - METAR trend rate computed over a mismatched time span;
  - plus side-dedup, `_yes_index`, partial resolution, local day and sea-breeze (listed elsewhere).

#### `ot-telegram-markdown` — "[" swallowed by Telegram Markdown
2026-06-12 · PR #69 · **adopted-in-code**
- **Result:** `Bucket bounds: [73.4, 75.2)` rendered wrong.
- **Decision:** "YES needs: 73.4°F (23.0°C) ≤ final max < 75.2°F (24.0°C)". Dual units for Celsius cities (Seoul).

#### `ot-test-harness-rules` — the test harness and what mutation testing found
2026-09-21 · PR #103, #104 · **adopted-in-code** (CLAUDE.md rules)
- **Rules:** no network (Tomorrow.io free tier "500 calls/day"; a leaking test burns quota); no Postgres; no Telegram; set a Telegram token or the test asserts nothing; `== False` in SQLAlchemy.
- **Result:** Mutation-tested against 9 deliberate breakages; 8 turned the suite red at once. The horizon test passed with `max_days_ahead_for_alert` deleted, "because a 10-day-out forecast is too uncertain to signal anyway", and was rewritten to sit on the boundary (day 3 trades, day 4 silent).
- **Observation:** a raising `get_station_bias` would make the bot go quiet with only a log line ("failing closed is right; failing silently is worth knowing").

#### `ot-harness-leak` — module-level collectors kept the first test's router
2026-09-24 · PR #112 · **adopted-in-code**
- **Result:** In the second of two flow tests, 0 of the detector's order-book calls reached that test's router (the self-check returned 0.10 from the old router instead of 0.90). Two tests had passed vacuously.

#### `ot-rollback-expires-orm` — one failing market lost the rest
2026-09-24 / 2026-09-27 · PR #111, #121 · **adopted-in-code**
- **Result:** A rollback inside a shared session expires every ORM object, so later markets or cities raised MissingGreenlet: "one bad market silently lost every later market that hour."
- **Decision:** Work from plain copies.

---

## (a) Incidents (real losses or bugs that led to a guard)

| date (PR created) | city | what happened | guard added | source |
|---|---|---|---|---|
| 2026-05-14 | London | "11°C" stored as 11°F; two contradictory YES alerts on one market | °C→°F at ingest (later native units); one alert per market | PR #23 |
| 2026-05-14 | (any) | "83°F or below YES 99%, median 90°F" from inverted old bounds | refresh bounds on re-ingest; skip "lowest" markets | PR #24 |
| 2026-05-16 | Atlanta, Dallas, NYC | 99% NO claims from 0/30 ensemble hits | Laplace + Gaussian blend, clip [3%, 97%] | PR #25 |
| 2026-05-17 | NYC | showed NO 86¢ while the market was 98¢ ($1 volume) | book-based entry cost; spread > 10¢ skip | PR #27 |
| 2026-05-20 | Dallas, SF | airport coordinates; today's wind used for a 1-day-ahead market | same-day-only observations; city edit UI | PR #28 |
| 2026-05-20 | London, Seattle, Miami, SF, Houston | 5 losses, all within ≤1°F of a bucket edge | boundary-proximity blend | PR #29 |
| 2026-05-22 | Austin, NYC, Houston | exclusive upper bound marked losses as wins | [min, max+1) resolution | PR #30 |
| 2026-05-23 | all | Meteosource '47.44N' coordinates crashed all opportunity detection | `_parse_coord` | PR #32 |
| 2026-05-25 | London | 32°C NO marked LOSS at 33.0°C (overlapping rounded-F ranges) | native °C bucket bounds | PR #35 |
| 2026-05-26 | Celsius cities | "29°C or higher" P(YES) ≈ 97% at 1–6¢ → spurious virtual buys | `resolve_bucket_unit` label fallback | PR #37, #38 |
| 2026-05-26 | — | deploy failed: migration > 120 s healthcheck | healthcheck 300 s | PR #36 |
| 2026-05-27 | Atlanta | two conflicting alerts 5 min apart | 30-min market cooldown | PR #38 |
| 2026-05-27 | Seoul / Seattle / São Paulo, Singapore / Austin | YES+NO same day; +67pp edge; overconfidence; METAR max ignored | any-side dedup (later reverted); max edge 0.45; sparse shrink; running-max override | PR #41 |
| 2026-05-29 | fleet | ~61% real vs ~95% stated; open-ended 4/13 | clip 0.92; σ widened; open-ended σ×1.5 | PR #45, #46 |
| 2026-05-30 | Hong Kong | P(NO)=91% with no forecast data | skip outcomes with no sources | PR #47 |
| 2026-05-30 | Seoul | P(NO)=97% with 4 sources missing | sparse baseline 5 global, 8%/missing | PR #48 |
| 2026-06-02 | Paris, Tokyo | exact-degree highs (22.0°C, 27.0°C) resolved wrong by float dust | `temp_in_bucket` rounding | PR #51 |
| 2026-06-02 | Dallas, Miami, Austin, Seoul, Hong Kong | METAR math and Polymarket parsing picked the wrong winner | Polymarket-only resolution, outcomes-index parsing, closed=true | PR #52–#55 |
| 2026-06-04/05 | Houston, Seoul, NY | METAR high contradicted the settled bucket; NY Jun 04 resolved from METAR | METAR fallback removed | PR #56, #57 |
| 2026-06-07 | — | dashboard hid older rows (200 cap) | cap 5000 + warning | PR #58 |
| 2026-06-11 | US cities | intraday UTC-midnight dedup allowed a double buy the same local day | local-day dedup | PR #67 |
| 2026-06-11 | Denver | WU showed 74°F, bot assumed 73.0°F | official running max = max(METAR, WU) | PR #68 |
| 2026-06-12 | Tokyo | intraday YES buy at 96% lost; max on the bucket floor, models Δ4.7°F | spread σ floor; pre-peak YES cap 90% | PR #69 |
| 2026-06-12 | Chicago | told to "switch" from 95% NO to an adjacent 83% NO | bucket-switch alerts YES-only | PR #69 |
| 2026-06-12 | Guangzhou | 3 re-alerts in 30 min (93→94→96) | 2pp + 15-min cooldown | PR #69 |
| 2026-06-12 | Paris | intraday NO at 98% lost; μ was mostly forecast with σ ±1.6 | forecast σ in quadrature; 96% stat cap | PR #70 |
| 2026-06-13 | Denver, Tokyo, Seoul, Paris, Hong Kong | "bot turned loss-making after several days" | WU-anchored bias; calibration gate (reverted); near-money; open-position dedup; METAR spike rejection | PR #72 |
| 2026-06-13 | Seoul, Tokyo | same bucket bought on day T and T+1, both lost | `_has_open_position` | PR #72 |
| 2026-06-13 | Hong Kong | METAR 89.6°F/32°C vs HKO/WU 84.7°F/29.3°C → false lock | spike > 5°F rejected; METAR > WU+4°F → WU | PR #72 |
| 2026-06-13 | Denver | 88°F forecast vs 92–93°F bucket → 93% NO auto-buy → loss | σ same-day 4.0°F; EWMA bias | PR #73 |
| 2026-06-14 | Taipei | "peak passed" at 14:14 on a 1.8°F dip → 96% → loss | `peak_confirm_hour` 15.5 | PR #75 |
| 2026-06-15 | London | held 21°C NO @ 60¢ and YES @ 74¢ | cross-bet coherence guard | PR #75 |
| 2026-06-22 | beta, fleet | beta ROI −13.4%, top confidence anti-predictive | shrink, σ floor 4, market blend 0.6 | PR #80 |
| 2026-06-23 | Seoul, HK, Dallas pattern | 99% intraday band won 78%, −$14.53 | unconfirmed-lock σ floor 2.0°F; cap 96 | PR #81 |
| 2026-06-23→26 | beta | beta alerts and buys fell to ~0/day | beta alert 0.80; beta buy 0.85 | PR #83, #88 |
| 2026-06-19→07-11 | all 48 cities | ICON never scheduled; Tomorrow.io rate-limited (144 vs 25/h) | ICON hourly; Tomorrow.io budget job; retry/backoff | PR #84, #88 |
| 2026-07-11 | Hong Kong | no model at all (no coordinates) | HKO coordinates migration | PR #88 |
| 2026-07-11 | Kuala Lumpur | ~55% city never tripped the streak rule | chronic win-rate suspension | PR #88 |
| 2026-07-11 | fleet | 7x% alerts despite threshold 80; settings reset on deploy | persistent settings; near alert 0.80 | PR #88 |
| 2026-07-21 | San Francisco | "7/5 global models"; contradictory buy lines | display fixes | PR #89 |
| 2026-07-25 | infra | memory heartbeat froze the bot ~96×/day | light diagnostics only | PR #94 |
| 2026-07-27 | infra | COUNT(*) diagnostic read ~3 GB per click | ANALYZE + reltuples | PR #100 |
| 2026-09-22 | infra | VACUUM FULL on 2,754 MB with ~750 MB free would fill the disk | 1,000 MB cap | PR #107 |
| 2026-09-23 | admin | every deploy logged the admin out (401) | signed tokens | PR #110 |
| 2026-09-24 | intraday | `lock_state` overflow lost the rest of the cycle (Postgres only) | VARCHAR(32) | PR #113 |
| 2026-09-24 | Lucknow, Seoul, Istanbul, Tel Aviv, HK | placeholder ICAO; METAR and WU on different airports; WU URL without a station | station audit | PR #114 |
| 2026-09-25 | Guangzhou | post-close shadow rows reversed the verdict | stop at local close | PR #117 |
| 2026-09-26 | infra | SQLAlchemy 2.1.1 without greenlet broke the deploy | version pins | PR #119 |
| 2026-09-28 | New York, London | 2025 markets stored as 2026; polluted shadow and model_skill | stale-event check; migration 028 | PR #124 |
| 2026-10-23 | infra | shadow export failed (aborted transaction) | rollback + per-day reads | PR #130 |
| 2026-11-17 | Munich | paper signal at mid 84¢ with ask 99.5¢ | tradable rule (then fair value) | PR #136, #139 |
| 2026-11-17 | nearby | no alerts at all after #136 (first sighting locked the market) | only a tradable signal closes a market | PR #137 |
| 2026-11-17/19 | SF, LA, Madrid | nearby signals fired before the peak / night warming → losses | after-usual-peak condition | PR #139 |
| 2026-11-22 | Jeddah; all | YES prices on a NO bet; "already held" flag on most buys; results 7–25 h late | side book; `_held_before`; hourly local-day settlement | PR #142 |
| 2026-11-26 | (unknown tokens) | 404 "No orderbook" log flood | rest bucket 1 h | PR #143 |

## (b) Tried and failed, reverted or replaced

| what | where | replaced or why | source |
|---|---|---|---|
| Gamma `?q=` text search for discovery | #15/#16 | "does NOT do real text search" → slug probing / `/markets` scan | PR #17–#20 |
| Signal-quality `compute_confidence` as alert gate (60%) | #1 | replaced by directional certainty 80% | PR #21 |
| One alert per market (best edge only) | #23 | multi-bucket alerting | PR #63 |
| Forever-dedup per (outcome, side) | #24 | daily dedup | PR #30 |
| Any-side dedup (Seoul fix) | #41 | reverted: "dedup ignored side — NO today blocked YES" | PR #62 |
| °C buckets converted to integer °F at ingest | #23 | overlapping ranges → native-unit bounds | PR #35 |
| Flat σ = 3°F, then lead σ 1.5–4.5, then 2.5-based | #25, #28, #46 | each still overconfident → 4.0-based schedule | PR #28, #46, #73 |
| Clip ceiling 97% | #25 | "entire batch at exactly 97%" → 92% | PR #46 |
| METAR-math resolution, then Polymarket-first with METAR fallback | #13, #52 | METAR disagreed with Polymarket → Polymarket only | PR #52, #57 |
| ICON "DB-only" phase | #40/#41 | blended in #42 (but never ran until #88) | PR #42, #88 |
| Calibration gate on buys | #72 | reverted to display-only the same day | PR #73 |
| Beta as specified at launch | #78 | anti-predictive, ROI −13.4% → shrink, σ floor, market blend | PR #80 |
| Beta Telegram gate 0.85 | #80 | silenced ~all beta signals → 0.80 | PR #83 |
| Beta buy gate 0.90 (shared with alpha) | #78 | unreachable after the blend (~88% cap) → 0.85 | PR #88 |
| "Transient API errors" as cause of ICON/Tomorrow.io no_data | #84 | real causes: never scheduled; quota burst | PR #88 |
| Open-position re-alert every run | #63 | spam → once/day, then ≥1pp | PR #64 |
| Intraday re-alert 1pp, no cooldown | #65/#66 | Guangzhou noise → 2pp + 15 min | PR #69 |
| WU in the intraday forecast blend | #65 | it is observed-so-far, dragged μ down → removed | PR #68 |
| Intraday peak-passed from 14:00 | #65 | Taipei false peak → confirm at 15.5 h | PR #75 |
| Bucket-switch alerts for NO positions | #63 | NO positions are complementary (Chicago) → YES-only | PR #69 |
| Daily same-day METAR running-max override (P(YES)→3% / ≥92%) | #41 | not found in current `probability_estimator.py`; no PR records its removal | — |
| Discretisation σ 0.289°F | #41 | not found in current `app/`; no PR records its removal | — |
| Memory heartbeat with `gc.collect()` + heap scan | #93 | froze the bot → light path | PR #94 |
| VACUUM FULL as the disk/RAM lever | #91, #95 | data is live (5,611 dead rows) → raw_data strip + size cap | PR #105, #107 |
| "BLOATED" diagnosis from pg estimates | #95 | estimates off ~4,900× | PR #97 |
| Exact COUNT(*) row-count button | #95 | cost money (~3 GB read/click) → ANALYZE | PR #100, #101 |
| Station audit verdict "47 of 48 mismatch" | #113 | #114 called it a parser bug; #129: the rules really had moved to weather.gov | PR #114, #129 |
| Shadow hour table → six-checkpoint grid → Brier lines | #111, #120, #122 | unreadable / unfair → hourly favourite + hours on winner | PR #122, #125 |
| "Market lags METAR by 5–15¢ after a lock" premise | #65 | locks: market priced ~80¢ and was right; Asia 26.11: "the market knew first" | PR #113, #143 |
| Whole-°C rounding explains the METAR-vs-settlement misses | #133 (hypothesis) | rejected: 60% vs 79% match | PR #133 |
| Settlement-station-only nearby rule | #134, #135 | −0.6 to +0.9¢; lost without neighbours | PR #134, #135 |
| Nearby truncating bucket rule | #135 | rounding matched 98.7%; truncating less | PR #135 |
| Nearby tradable = ask ≤88¢ & spread ≤10¢ | #136 | replaced by fair value − 2¢ | PR #139 |
| Kelly sizing | #62 (proposed) | deferred, never built | PR #62 |

## (c) Timeline

- **May 2026:** The bot is copied in and wired up (12 cities; discovery fixed after several failed attempts). First probability model: GFS ensemble → Laplace + Gaussian 70/30. HRRR, Tomorrow.io, Meteosource, ICON added. Liquidity and spread gate. First retrospective (May 20: losses at bucket edges) → boundary, disagreement and straddle blends. Celsius-unit bugs. Virtual 5-share buys. Late May: first calibration check (~61% vs ~95%) → clip 92%, σ widened, open-ended ×1.5, no-data skip, sparse-source shrink, airport +1.5°F bias.
- **June 2026:** Resolution saga, ending in Polymarket-only WIN/LOSS. Confidence bands (90–94% ROI −3.1%). Per-city model weights, per-source bias, ECMWF ensemble, model-skill DB with lead time. Calibrator (display only). Auto-suspension. **Intraday subsystem** launched (Jun 10) and hardened through field incidents (Denver WU, Tokyo, Paris, Taipei, London cross-bet, HK spike). WU-anchored and EWMA bias; σ same-day 4°F. **Beta estimator** (Jun 19) proves anti-predictive, gets repaired, then is starved by its own gates. Exit monitor. Collector retry.
- **July 2026:** Data-pipeline holes found from the 11.7 exports (ICON never scheduled, Tomorrow.io quota, HK coordinates). Chronic-loser suspension. Persistent settings. Then a **cost** sprint: forecast dedup, VACUUM, the GC-freeze hotfix, missing indexes, write-on-change prices, dead `_price_trend` queries, log noise, TOAST reads, tiered polling. DB growth −81% measured.
- **August 2026:** No PRs.
- **September 2026:** Test harness and CLAUDE.md rules. More cost work (raw_data strip, streaming CSV, VACUUM FULL cap, CPU per job). Suspension false-alarm analysis. **Shadow study** (model adds no info beyond market: z +0.32 vs +6.28). **Intraday analysis of 5,418 bets** (−1.88pp, t = −3.35) → lock margin and 70¢ floor. Station audit. Open-Meteo budget. Daily-peaks recording. 2025-markets-as-2026 bug.
- **October 2026:** Peak guard recorded but OFF (65% vs 78%, z = 3.0). Per-city forward-test tool (z > 3 rule). **Polymarket resolution moved to weather.gov**. Model v2 recorded in the shadow study (42% vs 34% bucket hit; +1.7 to +4.7¢ over 8 days). Shadow export fixes.
- **November 2026:** Model-run and 5-min price exports. weather.gov observations recorded (hourly METAR misses 21% of US days). **Nearby-stations** research → paper test (+6.5/+4.0/+4.5¢, decays within minutes), refined to after-peak + fair-value buying (OOS +8.4¢, t = 3.24). **Real track**: same-day ≥ 91% (backtest +3.0¢, t = 2.39); live slice ≥ 70¢ 81% vs < 70¢ 49%. Hourly local-day settlement.

---

## Numbers I could not verify or that conflict

1. **WU vs METAR direction.** #72: WU "runs 1-2°F warmer than METAR", so the bias should be anchored to WU. #74: "METAR runs 2-4°F above WU". #81: WU resolves "1–3°F lower" than METAR. These conflict in direction, unless one is about daily highs and the other about intraday spikes; no PR reconciles them.
2. **Resolution source.** #72, #74, #81 and #113 all build guards on "Polymarket resolves on Wunderground" (WU-anchored bias, WU-confirmed locks). #129 (2026-10-04) says Polymarket moved to the NWS weather.gov timeseries, and #133 says US markets settle on weather.gov. The code still says WU (`bias_estimator.py:3`), and the intraday max still scrapes WU. When the switch happened is not stated, and which guards rest on a stale premise is open.
3. **Station-audit verdict reversed twice.** #113 found 47/48 mismatches; #114 called that a parser bug ("it was the audit that was wrong, not the cities"); #129 says the rules really had changed.
4. **"Stations were fixed on 24 Oct"** (#133, `nwsobs/__init__.py`). The station-audit PRs are dated 24 Sep (#113, #114) and 4 Oct (#129). Possibly 24 Sep, or the owner applied fixes on 24 Oct. Unverifiable.
5. **NYC settlement station.** #4: KLGA → #15/#16: KNYC (Central Park) → `app/nearby/stations.py` (generated 2026-11-16) keys New York's settlement station as **KLGA**, with KNYC as a neighbour, while `app/utils/seed.py` still says KNYC. Likewise Dallas: #28 moved coordinates to the city centre ("markets resolve on city-centre temperature"), but the nearby table keys Dallas as **KDAL** with KDFW as a neighbour, and #134 says "the old Dallas station has no neighbours". The current DB stations are not visible in the repo.
6. **Clip "92% max".** #46 says no signal can exceed 92%, but the clip is on P(YES) = [0.03, 0.92], so NO certainty can still reach 97%. Later data show 95–100% bands (#60: 9 positions), "α 96% NO" (#89) and beta ~96% (#80).
7. **Daily calibration figures vary by period:**
   - ~61% real vs ~95% stated (#45, #46: 3 days);
   - 90–94% band at 72.5% (#60);
   - "raw conf 90→57% true" (#75), against a 71% overall win rate in the same CSV: unclear what 57% refers to;
   - "≥90% realised ~78%" (#108);
   - same-day ≥91% at 76.4% and 1-day-ahead ≥91% at 71.4% (#141).
8. **May 20 retrospective vs resolution bug.** #29 counts Houston 82-83°F NO (actual 82.9) as a loss, but #30 says the checker had recorded exactly that bet as a WIN. Austin 80–81 and NYC 68–69 were also wrong WINs. So the "26 wins / 5 losses (83.9%)" may not match what the DB held.
9. **Intraday sample sizes use different units and filters:**
   - 637 rows / 200 executed (#81, by Jun 23);
   - 500 "settled intraday trades" Jun–Jul ≥14:00 (#127);
   - 2,760 "settled intraday alerts" Jun–Jul (#121);
   - 5,418 "settled intraday bets" through Sep (#113).
   In #113, "58 of 67" sits next to "89 bets" with no statement of what the 67 are.
10. **Duplicate-index saving:** "~650 MB" (#97) vs "742 MB freed" (#100).
11. **#100 per-table growth does not add up:** "before" market_prices ~52 MB/day + forecasts ~38 MB/day = 90 MB/day, more than the stated total ~68 MB/day.
12. **Shape of the RAM curve:** #93 says it climbs "linearly" (an app leak); #106 says it is "step-shaped, not a smooth ramp" (exports). Bill shares are for different periods and not comparable: Postgres 97% ($23.71 vs $0.68, #101); RAM ~65% / CPU ~18% (#102); memory 67% ($9.47 of $14.13, #106); CPU 38% ($4.09 of $14.80, #110).
13. **market_prices size:** 2,963 MB of 3,871 MB (#95, Jul 25) vs 2,754 MB of 3,892.3 MB (#105, Sep 22); 17,532,604 rows at 177 B/row (#97) vs 21,477,844 at ~134 B/row (#105). Consistent with the index drop, but no PR reconciles them.
14. **Kuala Lumpur archetype:** "55%" (#88) vs "W-L-W-L-L-W at 56%" (`opportunity_detector.py:144`).
15. **88¢ entry-cap arithmetic:** "risking $4.40 to win ~40¢ … destroys 10 wins" (`config.py`) vs "At 92¢ entry we risk $4.60 to win $0.40 … wipes out 11 wins" (`detector.py:696`). Both are arithmetic, not measurement.
16. **Nearby research vs production:** research used "up to 6 nearby stations in 35 cities" (#135); production collects "up to five" per station for 32 settlement stations, 120 in all (#134). `NEARBY_HISTORY` covers 113 of 120.
17. **Real-track ≤90% edge:** 63.7% − 68.6¢ = −4.9 **(derived)**, but #141 states −4.8¢. Probably rounding of the unrounded inputs.
18. **ICON status:** #40/#41 "DB-only, not blended"; #42 blended; #47 says the "DB-only" docstrings were stale. `config.py:33-34` still says "NOT yet mixed into the deterministic blend" (stale). #88 shows ICON never ran in production until 2026-07-11, so ICON contributed nothing to May–July results despite being "in the blend".
19. **#87 vs #88:** identical "6 fixes from the analysis of the 11.7 files" body, but #87 is dated created/merged 2026-06-26, two weeks before 11 July. The fixes are dated here at #88 (2026-07-11); #87's body was probably edited later.
20. **Removed without a record:** the daily same-day METAR running-max override (#41: P(YES)→3% / ≥92%) and the discretisation σ (#41: 0.289°F) are not in the current `app/`, and no PR says when or why they went.
21. **Likely illustrative, not measurements:**
    - #59 threshold table (45 / 38 / 28 / 73.7% …);
    - #108 sample suspension report (Austin 34 / 7/10, Chicago 28 / 5/10);
    - #110 sample job table (polymarket 49.0% CPU, analyzer 38.1%);
    - example alerts in #22, #28 and #86.
22. **Paris "profitable".** #128 asks "what makes Paris profitable", but no PR gives Paris's figures. Paris also appears as the blacklist example (#51) and in the intraday 98% loss (#70).
23. **#46's predicted effect** ("gap should narrow from ~35pp toward ~15-20pp") and **#90's predicted cost** (~$13–16/month) were never confirmed as measured outcomes in a later PR. The later bills are $14.13 (#106) and $14.80 (#110), but for unstated periods.
