"""רגרסיה לתקרית פריז (12 ביוני, 21:03 UTC).

ההודעה מהשטח:
    98% ⚡ INTRADAY UPDATE — NO 25°C (Paris)
    running max 66.2°F → expected 73.8°F | σ=±1.6°F
    Entry: 84¢ | Certainty: 98%   ← הפסיד. היום עקף את כל המודלים.

שורש הבעיה: 5.2 שעות לסוף השיא, μ נשען כמעט כולו על תחזית (פער של
7.6°F בין המקסימום הרץ לצפי) — אבל σ נלקח מלוח-הזמנים בלבד (±1.6°F),
כאילו התחזית עצמה מושלמת. שגיאת תחזית של אותו יום היא ±2.5°F — והיא
חייבת להיכנס ל-σ ביחס לתלות בתחזית (w).

שני התיקונים:
1. σ_eff = sqrt(σ_לוח² + (w·2.5)²) — חיבור ריבועי של שני מקורות אי-ודאות.
2. תקרה סטטיסטית 96%: אומדן שאינו נעילה ואינו אחרי-שיא לעולם לא טוען
   ביטחון קיצוני — יש זנבות שהמודל לא רואה.
"""
import pytest

from app.intraday.estimator import DEFAULT_PARAMS, PROB_HI, PROB_LO, estimate_intraday

# הקלט המדויק מההתראה: מקסימום רץ 66.2°F, expected final 73.8 משמעו
# שהתחזית המשולבת היתה ‎≈76.4°F (כי μ = M + w·(F−M) עם w=0.743).
PARIS = dict(
    running_max_f=66.2, current_temp_f=66.2, minutes_since_max=20.0,
    forecast_high_f=76.4, local_hour=11.8,           # 5.2 שעות לסוף השיא
    bucket_min=25, bucket_max=25, bucket_unit="C",   # [77.0, 78.8)°F
)


def test_paris_regression_no_more_98pct():
    """השחזור המדויק: הביטחון של NO חייב לרדת מתחת לסף הקנייה (94%)."""
    p, bd = estimate_intraday(**PARIS)
    no_certainty = 1.0 - p
    assert no_certainty < 0.94, (
        f"NO certainty {no_certainty:.3f} still above buy threshold — "
        f"the Paris failure mode is back"
    )
    # μ משחזר את ההודעה המקורית (expected final ≈ 73.8°F)
    assert bd["expected_final_max_f"] == pytest.approx(73.8, abs=0.3)
    # הסיגמה התרחבה הרבה מעבר ל-±1.6 של לוח-הזמנים
    assert bd["sigma_used"] > 2.0
    assert bd["sigma_forecast_term"] > 1.5


def test_forecast_term_vanishes_when_max_already_measured():
    """אחרי השיא אין תלות בתחזית — איבר שגיאת-התחזית חייב להיעלם."""
    p, bd = estimate_intraday(
        running_max_f=85.0, current_temp_f=82.0, minutes_since_max=120.0,
        forecast_high_f=85.0, local_hour=16.0,
        bucket_min=84, bucket_max=85, bucket_unit="F",
    )
    assert bd["peak_passed"] is True
    assert bd["sigma_forecast_term"] == 0.0
    assert bd["sigma_used"] == DEFAULT_PARAMS.post_peak_sigma


def test_forecast_term_shrinks_through_the_day():
    """w דועך לאורך היום ⇒ התלות בתחזית — והאיבר שלה — מתכווצים."""
    def fc_term(hour):
        _, bd = estimate_intraday(
            running_max_f=70.0, current_temp_f=70.0, minutes_since_max=15.0,
            forecast_high_f=78.0, local_hour=hour,
            bucket_min=80, bucket_max=81, bucket_unit="F",
        )
        return bd["sigma_forecast_term"]
    assert fc_term(10.5) > fc_term(13.0) > fc_term(16.0)


def test_stat_cap_blocks_extreme_claims_pre_peak():
    """דלי רחוק מאוד לפני סוף-השיא: ביטחון נחתך ב-96%, לא 98.5%."""
    p, bd = estimate_intraday(
        running_max_f=66.0, current_temp_f=66.0, minutes_since_max=20.0,
        forecast_high_f=70.0, local_hour=11.0,
        bucket_min=95, bucket_max=96, bucket_unit="F",
    )
    assert p == DEFAULT_PARAMS.stat_prob_lo        # ולא PROB_LO=0.015
    assert bd["stat_cap_applied"] is True
    assert (1.0 - p) <= 0.96 + 1e-9


def test_stat_cap_does_not_touch_locks():
    """נעילות הן מתמטיות — שומרות על 98.5% המלא."""
    p, bd = estimate_intraday(
        running_max_f=86.4, current_temp_f=86.0, minutes_since_max=20.0,
        forecast_high_f=87.0, local_hour=12.0,
        bucket_min=86, bucket_max=None, bucket_unit="F",
    )
    assert p == PROB_HI
    assert bd["stat_cap_applied"] is False

    p2, bd2 = estimate_intraday(
        running_max_f=85.0, current_temp_f=84.0, minutes_since_max=30.0,
        forecast_high_f=86.0, local_hour=12.0,
        bucket_min=78, bucket_max=79, bucket_unit="F",
    )
    assert p2 == PROB_LO
    assert bd2["stat_cap_applied"] is False


def test_stat_cap_does_not_touch_post_peak():
    """אחרי השיא σ=0.3 אמין — האומדן כמעט-מתמטי ואינו נחתך."""
    p, bd = estimate_intraday(
        running_max_f=78.8, current_temp_f=76.0, minutes_since_max=150.0,
        forecast_high_f=78.0, local_hour=16.5,
        bucket_min=78, bucket_max=79, bucket_unit="F",
    )
    assert bd["peak_passed"] is True
    assert bd["stat_cap_applied"] is False
    assert p > 0.96  # הדלי מכיל את המקסימום והטמפ' יורדת — באמת כמעט ודאי
