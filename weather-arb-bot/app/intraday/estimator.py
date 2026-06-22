"""Pure intraday probability model. See INTRADAY.md for the full strategy.

The final daily max is modeled as max(M, X):
- M = the running METAR max so far today (monotonic, known)
- X ~ N(mu, sigma_h) = the max that remaining heating would reach

mu anchors on M plus the time-decayed share of (blended forecast high - M).
sigma_h shrinks as the local clock approaches the end of the climatological
peak window, and collapses further once the peak has demonstrably passed.

Every constant lives in IntradayParams so the learning loop can tune them.
All functions here are pure — no DB, no I/O — and fully unit-testable.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

from app.analyzers.probability_estimator import _bucket_to_f_bounds, _norm_cdf

PROB_LO = 0.015
PROB_HI = 0.985


@dataclass(frozen=True)
class IntradayParams:
    start_hour: float = 10.0        # don't run the intraday view before this local hour
    peak_start_hour: float = 14.0   # climatological window in which the max occurs
    peak_end_hour: float = 17.0
    # ── תקרית טאיפיי (14 ביוני) ───────────────────────────────────────────
    # "peak passed" הוכרז ב-14:14 — 14 דקות בלבד לתוך חלון השיא — על סמך
    # ירידה זמנית של 1.8°F. σ קרס ל-0.3°F, הביטחון טיפס ל-96%, והטמפרטורה
    # המשיכה לעלות תוך שעה וההימור נפל. השיא היומי האמיתי מגיע לרוב
    # 14:00-16:00, ולכן אסור להכריז "השיא עבר" בתחילת החלון על סמך ירידה
    # רגעית. זיהוי-הקירור מורשה רק מהשעה הזו ואילך; לפניה המקסימום עוד
    # יכול לברוח כלפי מעלה והמודל שומר על σ רחב מלוח-הזמנים. הקירור עצמו
    # (cooling_drop_f / cooling_min_minutes) עדיין נדרש בנוסף לתנאי הזה.
    peak_confirm_hour: float = 15.5
    # (hours_to_peak_end_at_least, sigma) — first matching row wins, ordered desc.
    sigma_schedule: tuple = (
        (6.0, 2.2),
        (4.0, 1.6),
        (2.0, 1.0),
        (1.0, 0.6),
        (0.0, 0.4),
    )
    post_peak_sigma: float = 0.3
    # Celsius buckets are only 1°C = 1.8°F wide. WU rounds temperatures to
    # the nearest °C, so a 0.5°C measurement difference between METAR and WU
    # can flip the winning bucket. We keep σ ≥ this floor for all Celsius
    # markets so that the model never manufactures >94% confidence on a YES bet
    # that hinges on a single WU decimal. Applies to both YES and NO sides
    # (a NO lock that only METAR confirmed is handled separately via wu_confirmed).
    celsius_min_sigma_f: float = 1.8   # 1°C expressed in °F
    # "Peak passed" detection: current temp this far below the running max...
    cooling_drop_f: float = 1.5
    # ...for at least this long, and only after peak_start_hour.
    cooling_min_minutes: float = 90.0
    # מודל שחולק על מודל = אי-ודאות אמיתית על החימום שנותר:
    # רצפת סיגמה = gain_weight * פיזור-המקורות * spread_sigma_weight.
    # (תקרית טוקיו: המקורות נפרשו על 4.7°F אבל סיגמה טענה ±1.0 — המודל
    # הכריז 96% על דלי שהמקסימום הרץ ישב בדיוק על הרצפה שלו.)
    spread_sigma_weight: float = 0.5
    # YES לא-נעול לפני שחלון השיא בכלל נפתח לא יכול להיות "כמעט-נעילה":
    # המקסימום עדיין יכול לברוח כלפי מעלה מהדלי. התקרה מתחת לסף הקנייה
    # כדי שמקרי קצה יתריעו אבל לעולם לא ייקנו אוטומטית.
    pre_peak_yes_cap: float = 0.90
    # ── תקרית פריז (12 ביוני) ─────────────────────────────────────────────
    # שגיאת תחזית של אותו יום: כש-μ נשען על תחזית (ולא על המקסימום שכבר
    # נמדד), אי-הוודאות חייבת לכלול את שגיאת התחזית עצמה. בפריז: מקסימום רץ
    # 66.2°F, צפי סופי 73.8°F — כלומר μ היה כמעט כולו תחזית — אבל σ נלקח
    # מלוח הזמנים בלבד (±1.6°F) והבוט הכריז NO ב-98%... והיום עקף את כל
    # המודלים והדלי "הבלתי-אפשרי" זכה. ±2.5°F הוא סדר הגודל המקובל לשגיאת
    # תחזית מקסימום של אותו יום.
    same_day_forecast_sigma: float = 2.5
    # תקרה סטטיסטית: אומדן שאינו נעילה מתמטית ואינו אחרי-שיא לעולם לא
    # מורשה לטעון יותר מ-96% (או פחות מ-4% ל-YES) — יש זנבות שהמודל
    # לא רואה (אדבקציה, פערי תחנות, הטיה ספציפית ליום).
    stat_prob_hi: float = 0.96
    stat_prob_lo: float = 0.04
    # ── תקרית סיאול / הונג-קונג / דאלאס (METAR-vs-WU divergence) ─────────
    # כשהMETAR עוקף את תקרת הדלי אבל WU (מקור הסטלמנט) עוד לא אישר —
    # lock_state = "yes_impossible_unconfirmed". עם σ=0.3 אפילו פער של 1°F
    # מייצר ביטחון NO של 98.5% (99 conf). WU אז מציג ערך נמוך יותר וההימור
    # מפסיד. הפיזור הטיפוסי METAR-WU הוא 1-3°F, לכן σ המינימלי לאי-מאושרים
    # הוא 2.0°F — מה שמבטיח שפער של 2°F ייתן ביטחון NO של ~84% בלבד (מתחת
    # לסף הקנייה של 94%), ולא 99% כפי שהיה קודם.
    unconfirmed_lock_sigma_f: float = 2.0


DEFAULT_PARAMS = IntradayParams()


def local_decimal_hour(now_utc: datetime, tz) -> float:
    """City-local clock as a decimal hour (e.g. 14.5 = 14:30)."""
    local = now_utc.astimezone(tz)
    return local.hour + local.minute / 60.0


def hours_to_peak_end(local_hour: float, params: IntradayParams = DEFAULT_PARAMS) -> float:
    return max(0.0, params.peak_end_hour - local_hour)


def gain_weight(local_hour: float, params: IntradayParams = DEFAULT_PARAMS) -> float:
    """Fraction of the day's remaining heating still ahead.

    1.0 at start_hour, decaying linearly to 0.0 at peak_end_hour. Clamped.
    """
    span = params.peak_end_hour - params.start_hour
    if span <= 0:
        return 0.0
    w = (params.peak_end_hour - local_hour) / span
    return max(0.0, min(1.0, w))


def expected_final_max(
    running_max_f: float,
    forecast_high_f: Optional[float],
    local_hour: float,
    params: IntradayParams = DEFAULT_PARAMS,
) -> float:
    """mu of the final-max distribution. Never below the running max."""
    if forecast_high_f is None or forecast_high_f <= running_max_f:
        return running_max_f
    remaining = (forecast_high_f - running_max_f) * gain_weight(local_hour, params)
    return running_max_f + remaining


def is_peak_passed(
    local_hour: float,
    current_temp_f: Optional[float],
    running_max_f: float,
    minutes_since_max: Optional[float],
    params: IntradayParams = DEFAULT_PARAMS,
) -> bool:
    """True when the day's max is very unlikely to rise further.

    Requires all three: we're far enough into the peak window that an early-
    afternoon dip can't be the heating merely pausing (peak_confirm_hour), the
    current temp has fallen well below the max, and the max was set long enough
    ago that the drop isn't just METAR noise.

    The gate is peak_confirm_hour (not peak_start_hour): the climatological max
    typically lands 14:00-16:00, so a cooling signal at the very start of the
    window (Taipei 14:14) is unreliable — the temperature routinely resumes
    climbing. Collapsing σ on that false signal is what produced the 96%
    wrong-direction confidence. Before peak_confirm_hour we keep the schedule σ.
    """
    if local_hour < params.peak_confirm_hour:
        return False
    if current_temp_f is None or minutes_since_max is None:
        return False
    return (
        running_max_f - current_temp_f >= params.cooling_drop_f
        and minutes_since_max >= params.cooling_min_minutes
    )


def intraday_sigma(
    local_hour: float,
    peak_passed: bool,
    params: IntradayParams = DEFAULT_PARAMS,
) -> float:
    if peak_passed:
        return params.post_peak_sigma
    h = hours_to_peak_end(local_hour, params)
    for min_hours, sigma in params.sigma_schedule:
        if h >= min_hours:
            return sigma
    return params.sigma_schedule[-1][1]


def lock_state(
    running_max_f: float,
    f_lo: Optional[float],
    f_hi: Optional[float],
    wu_confirmed: bool = True,
) -> Optional[str]:
    """Deterministic outcomes already decided by the monotonic running max.

    - "yes_impossible": the running max is already above the bucket's top —
      this bucket cannot be the final answer (the max can only rise).
    - "yes_locked": open-ended ">=lo" bucket whose floor was already touched —
      it is guaranteed YES regardless of what happens next.

    wu_confirmed: True when the running_max came from the Wunderground station
    (the Polymarket resolution source). When False, we have only a METAR reading.
    METAR and WU can diverge by 2-4°F, so a METAR-only lock is not trustworthy —
    yes_impossible is suppressed in that case to prevent locking a bucket the WU
    station has not yet confirmed.  yes_locked is unaffected (it only fires for
    open-ended >= buckets where METAR above the floor is safe).
    """
    if f_hi is not None and running_max_f >= f_hi:
        if wu_confirmed:
            return "yes_impossible"
        # METAR exceeded the ceiling but WU hasn't confirmed — treat as a
        # high-confidence statistical signal, not a mathematical lock.
        return "yes_impossible_unconfirmed"
    if f_hi is None and f_lo is not None and running_max_f >= f_lo:
        return "yes_locked"
    return None


def bucket_probability(
    running_max_f: float,
    mu: float,
    sigma: float,
    f_lo: Optional[float],
    f_hi: Optional[float],
    wu_confirmed: bool = True,
) -> float:
    """P(final max lands in [f_lo, f_hi)) under final = max(M, X), X~N(mu, sigma).

    The mass of X below M collapses onto the point M (the max can't go down),
    which gives clean closed forms:
    - bucket entirely below M           -> ~0
    - bucket containing M (lo <= M < hi)-> Phi((hi - mu) / sigma)
    - bucket above M (lo > M)           -> Phi((hi-mu)/s) - Phi((lo-mu)/s)
    Open-ended tails follow the same logic with the missing bound at +/-inf.

    wu_confirmed controls whether a running_max above f_hi is treated as a
    mathematical lock ("yes_impossible") or falls through to the statistical
    path ("yes_impossible_unconfirmed"). Pass False when running_max is from
    METAR only — WU (the resolution source) may resolve at a different value.
    """
    sigma = max(sigma, 1e-6)

    state = lock_state(running_max_f, f_lo, f_hi, wu_confirmed=wu_confirmed)
    if state == "yes_impossible":
        return PROB_LO
    if state == "yes_locked":
        return PROB_HI
    # "yes_impossible_unconfirmed" falls through to the statistical path —
    # the running max is only from METAR (WU not yet available/fresh), so we
    # use sigma (widened by the unconfirmed_lock_sigma_f override in
    # estimate_intraday) to reflect WU-METAR divergence uncertainty.

    if f_hi is None:
        # ">= lo" not yet touched (lo > M): P(X >= lo)
        p = 1.0 - _norm_cdf((f_lo - mu) / sigma)
    elif f_lo is None or f_lo <= running_max_f:
        # "<= hi" or bucket containing the running max: everything below hi
        # (including the point mass at M) counts.
        p = _norm_cdf((f_hi - mu) / sigma)
    else:
        p = _norm_cdf((f_hi - mu) / sigma) - _norm_cdf((f_lo - mu) / sigma)

    return max(PROB_LO, min(PROB_HI, p))


def estimate_intraday(
    running_max_f: float,
    current_temp_f: Optional[float],
    minutes_since_max: Optional[float],
    forecast_high_f: Optional[float],
    local_hour: float,
    bucket_min: Optional[float],
    bucket_max: Optional[float],
    bucket_unit: str = "F",
    params: IntradayParams = DEFAULT_PARAMS,
    metar_max_f: Optional[float] = None,
    forecast_spread_f: Optional[float] = None,
    wu_confirmed: bool = True,
) -> Tuple[float, dict]:
    """Full intraday estimate for one bucket. Returns (probability, breakdown).

    running_max_f is the OFFICIAL running max (may come from Wunderground, the
    resolution source). metar_max_f, when given, is the METAR-derived max used
    only for peak-passed detection — current_temp_f and minutes_since_max are
    METAR readings, so the cooling test must compare on the same scale.
    forecast_spread_f is the max-min spread of the (bias-corrected) source
    forecasts: disagreement about the remaining heating widens sigma.
    """
    f_lo, f_hi = _bucket_to_f_bounds(bucket_min, bucket_max, bucket_unit)

    peak_detection_max = metar_max_f if metar_max_f is not None else running_max_f
    peak_passed = is_peak_passed(
        local_hour, current_temp_f, peak_detection_max, minutes_since_max, params
    )
    w = gain_weight(local_hour, params)
    sigma_schedule = intraday_sigma(local_hour, peak_passed, params)

    # Pass wu_confirmed to lock_state so METAR-only readings don't trigger a
    # hard mathematical lock (yes_impossible). The lock fires only when the
    # Wunderground station — the Polymarket resolution source — confirms it.
    state = lock_state(running_max_f, f_lo, f_hi, wu_confirmed=wu_confirmed)

    # ── הרכבת הסיגמה האפקטיבית (תיקון תקרית פריז) ────────────────────────
    # שני מקורות אי-ודאות בלתי-תלויים, ולכן מחוברים ריבועית (quadrature):
    #   1. רעש תוך-יומי לפי לוח הזמנים (מתכווץ ככל שמתקרבים לסוף השיא)
    #   2. שגיאת התחזית עצמה — נכנסת באופן יחסי לכמה ש-μ תלוי בתחזית:
    #      μ = M + w·(F − M), ולכן שגיאה ב-F מתורגמת ל-w·שגיאה ב-μ.
    #      בבוקר (w≈1) אנחנו בעצם חוזים תחזית — σ חייב להיות רחב;
    #      אחרי השיא (w=0) המקסימום כבר נמדד — האיבר נעלם מעצמו.
    # מעל שניהם: רצפה מאי-הסכמת מודלים (תקרית טוקיו) — אם המקורות עצמם
    # חלוקים, אסור לסיגמה להיות צרה מהמחלוקת המשוקללת.
    sigma_fc_term = 0.0
    sigma_floor = 0.0
    if not peak_passed:
        sigma_fc_term = w * params.same_day_forecast_sigma
        sigma = math.sqrt(sigma_schedule ** 2 + sigma_fc_term ** 2)
        if forecast_spread_f and forecast_spread_f > 0:
            sigma_floor = w * float(forecast_spread_f) * params.spread_sigma_weight
            sigma = max(sigma, sigma_floor)
    else:
        # אחרי שהשיא עבר המקסימום כבר נקבע במציאות — אין תלות בתחזית.
        sigma = sigma_schedule

    # Celsius bucket precision floor: WU rounds to the nearest °C (1.8°F), so
    # a 0.5°C METAR-vs-WU measurement difference can flip the winning bucket.
    # σ < 1.8°F implies false precision — the model cannot distinguish
    # adjacent Celsius buckets reliably. Floor applies after peak too.
    celsius_floor_applied = False
    if bucket_unit == "C" and sigma < params.celsius_min_sigma_f:
        sigma = params.celsius_min_sigma_f
        celsius_floor_applied = True

    # ── Unconfirmed-lock sigma override (Seoul/HK/Dallas fix) ─────────────────
    # When METAR exceeded the bucket ceiling but WU hasn't confirmed it yet,
    # the typical 1-3°F METAR-WU divergence dominates over schedule noise.
    # Applying σ=0.3°F here claims 99% NO confidence based on a single METAR
    # reading — which then loses when WU resolves at a lower value. The 2°F
    # floor means a 2°F METAR excess yields ~84% NO certainty (below 94% buy
    # threshold) rather than the former 99%.
    unconfirmed_sigma_applied = False
    if state == "yes_impossible_unconfirmed" and sigma < params.unconfirmed_lock_sigma_f:
        sigma = params.unconfirmed_lock_sigma_f
        unconfirmed_sigma_applied = True

    mu = expected_final_max(running_max_f, forecast_high_f, local_hour, params)
    # Pass wu_confirmed so bucket_probability uses the same lock logic we did
    # above — unconfirmed METAR-only readings must NOT pin p at PROB_LO.
    p = bucket_probability(running_max_f, mu, sigma, f_lo, f_hi, wu_confirmed=wu_confirmed)
    # state was already computed above. Do NOT recompute here.

    # ── תקרת YES לפני חלון השיא ──────────────────────────────────────────
    # לפני שחלון השיא הקלימטולוגי נפתח, דלי לא-נעול עדיין יכול לאבד את
    # ה-YES שלו לחימום נוסף — לא משנה כמה הסיגמה צרה. נעילות פטורות
    # (הן מתמטיות, לא סטטיסטיות). unconfirmed locks treated like no lock here.
    pre_peak_cap_applied = False
    if (
        state is None
        and local_hour < params.peak_start_hour
        and p > params.pre_peak_yes_cap
    ):
        p = params.pre_peak_yes_cap
        pre_peak_cap_applied = True

    # ── תקרה סטטיסטית (תיקון תקרית פריז, חלק ב') ─────────────────────────
    # אומדן שאינו נעילה ואינו אחרי-שיא הוא ניחוש סטטיסטי שתלוי בתחזית —
    # לעולם לא מורשה לטעון ביטחון קיצוני. 98% על משהו תוך-יומי שעוד
    # תלוי ב-5 שעות חימום עתידי הוא חוסר ענווה, לא מודל.
    # Unconfirmed locks (METAR-only) are statistical even after peak: WU can
    # still resolve at a different value. The cap applies to them too.
    # Normal post-peak (lock_state=None) is exempt: running max inside/below
    # the bucket with confirmed peak is genuinely high-confidence.
    stat_cap_applied = False
    _is_hard_lock = state in ("yes_impossible", "yes_locked")
    _is_unconfirmed = state == "yes_impossible_unconfirmed"
    if not _is_hard_lock and (not peak_passed or _is_unconfirmed):
        if p > params.stat_prob_hi:
            p = params.stat_prob_hi
            stat_cap_applied = True
        elif p < params.stat_prob_lo:
            p = params.stat_prob_lo
            stat_cap_applied = True

    breakdown = {
        "running_max_f": round(running_max_f, 1),
        "metar_max_f": round(metar_max_f, 1) if metar_max_f is not None else None,
        "current_temp_f": round(current_temp_f, 1) if current_temp_f is not None else None,
        "forecast_high_f": round(forecast_high_f, 1) if forecast_high_f is not None else None,
        "expected_final_max_f": round(mu, 1),
        "local_hour": round(local_hour, 2),
        "hours_to_peak_end": round(hours_to_peak_end(local_hour, params), 2),
        "gain_weight": round(w, 3),
        "sigma_used": round(sigma, 3),
        "sigma_schedule": round(sigma_schedule, 3),
        "sigma_forecast_term": round(sigma_fc_term, 3),
        "sigma_floor_from_spread": round(sigma_floor, 3),
        "forecast_spread_f": (
            round(float(forecast_spread_f), 1) if forecast_spread_f is not None else None
        ),
        "pre_peak_cap_applied": pre_peak_cap_applied,
        "stat_cap_applied": stat_cap_applied,
        "celsius_floor_applied": celsius_floor_applied,
        "unconfirmed_sigma_applied": unconfirmed_sigma_applied,
        "wu_confirmed": wu_confirmed,
        "peak_passed": peak_passed,
        "lock_state": state,
        "f_lo": f_lo,
        "f_hi": f_hi,
        "probability": round(p, 4),
    }
    return p, breakdown
