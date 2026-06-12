"""בדיקות למאגר דיוק-המודלים הפר-עירוני (model_skill).

מכסות את הפונקציות הטהורות — הניקוד מול הדלי המנצח, נוסחת המשקל,
המרת גבולות דלי ל-°F — ואת השקלול בפועל בבלנד התוך-יומי.

רגרסיה קריטית: בלי רשומות כישרון ההתנהגות חייבת להיות זהה בדיוק
להתנהגות הישנה (משקל ניטרלי 1.0 לכולם) — שום שינוי במודל החיזוי
עד שמצטברות ראיות אמיתיות.
"""
import pytest

from app.analyzers.model_skill import (
    MIN_SAMPLES,
    SKILL_SOURCES,
    SOURCE_TO_SIGNAL_KEY,
    _bucket_f_interval,
    score_forecast,
    skill_weight,
)
from app.intraday.detector import _BLEND_WEIGHTS, blended_forecast_high


# ── המרת גבולות דלי ל-°F ─────────────────────────────────────────────────────

def test_bucket_interval_fahrenheit():
    # דלי "76-77°F" = [76, 78) — bmax כולל ולכן +1
    assert _bucket_f_interval("F", 76, 77) == (76.0, 78.0)


def test_bucket_interval_celsius():
    # דלי "25°C" = [25, 26)°C = [77.0, 78.8)°F
    lo, hi = _bucket_f_interval("C", 25, 25)
    assert lo == pytest.approx(77.0)
    assert hi == pytest.approx(78.8)


def test_bucket_interval_open_ended():
    lo, hi = _bucket_f_interval("F", 90, None)   # "90°F+"
    assert lo == 90.0 and hi is None
    lo2, hi2 = _bucket_f_interval("F", None, 60)  # "60°F or lower"
    assert lo2 is None and hi2 == 61.0


# ── ניקוד תחזית מול הדלי המנצח ───────────────────────────────────────────────

def test_score_hit_inside_winning_bucket():
    hit, dist, signed = score_forecast(76.5, [("F", 76, 77)])
    assert hit is True and dist == 0.0 and signed == 0.0


def test_score_miss_above_is_positive_bias():
    # המודל חזה 80 כשהדלי המנצח הוא [76, 78) — פספוס של 2 מעלות מעל
    hit, dist, signed = score_forecast(80.0, [("F", 76, 77)])
    assert hit is False
    assert dist == pytest.approx(2.0)
    assert signed == pytest.approx(2.0)      # חיובי = חוזה גבוה מדי


def test_score_miss_below_is_negative_bias():
    hit, dist, signed = score_forecast(73.0, [("F", 76, 77)])
    assert hit is False
    assert dist == pytest.approx(3.0)
    assert signed == pytest.approx(-3.0)     # שלילי = חוזה נמוך מדי


def test_score_takes_nearest_winner_when_multiple():
    winners = [("F", 70, 71), ("F", 76, 77)]
    hit, dist, signed = score_forecast(75.0, winners)
    assert hit is False
    assert dist == pytest.approx(1.0)        # הקרוב: רצפת [76,78)


def test_score_celsius_winning_bucket():
    # פריז: הדלי המנצח 25°C = [77.0, 78.8)°F; המודל חזה 73.8°F
    hit, dist, signed = score_forecast(73.8, [("C", 25, 25)])
    assert hit is False
    assert dist == pytest.approx(3.2)
    assert signed == pytest.approx(-3.2)


def test_score_none_without_winners():
    assert score_forecast(75.0, []) is None


# ── נוסחת המשקל ──────────────────────────────────────────────────────────────

def test_weight_neutral_below_min_samples():
    """אין ענישה על חוסר היסטוריה — מתחת ל-MIN_SAMPLES תמיד 1.0."""
    for n in range(MIN_SAMPLES):
        assert skill_weight(n, n) == 1.0


def test_weight_formula_matches_legacy():
    """אותה נוסחה בדיוק כמו model_weights הוותיק — אפס שינוי התנהגות."""
    from app.analyzers.model_weights import weights_from_tallies
    legacy = weights_from_tallies({"k": (8, 10)})["k"]
    assert skill_weight(8, 10) == legacy


def test_weight_bounds():
    assert 0.5 <= skill_weight(0, 100) < 1.0 < skill_weight(100, 100) <= 1.5


def test_source_signal_key_mapping_complete():
    """כל מקור נמדד חייב מיפוי למפתח-הסיגנלים שהאסטימטורים מכירים."""
    for s in SKILL_SOURCES:
        assert SOURCE_TO_SIGNAL_KEY[s] == f"{s}_forecast"
    # ביטחון: WU בכוונה לא נמדד (ביום האירוע ה"תחזית" שלו היא תצפית)
    assert "wunderground" not in SKILL_SOURCES


# ── השקלול בבלנד התוך-יומי ───────────────────────────────────────────────────

def _signals(weights=None):
    sig = {
        "station_bias": {"bias_f": 0.0001, "per_source": {}},   # כמעט-אפס: בידוד השקלול
        "hrrr_forecast": {"predicted_high_f": 70.0},
        "gfs_forecast": {"predicted_high_f": 80.0},
    }
    if weights is not None:
        sig["model_weights"] = weights
    return sig


def test_blend_neutral_without_skill_records():
    """רגרסיה: בלי משקולות כישרון הבלנד זהה להתנהגות הישנה."""
    base = blended_forecast_high(_signals())
    with_empty = blended_forecast_high(_signals(weights={}))
    assert base == with_empty


def test_blend_shifts_toward_skilled_model():
    """מודל שמוכח כמדויק בעיר מקבל יותר השפעה על הצפי."""
    neutral = blended_forecast_high(_signals())
    # GFS (החוזה 80) מקבל משקל כישרון 1.5; HRRR יורד ל-0.5
    skewed = blended_forecast_high(_signals(weights={
        "gfs_forecast": 1.5, "hrrr_forecast": 0.5,
    }))
    assert skewed > neutral          # הבלנד נע לכיוון המודל המדויק
    # החישוב המפורש: (2.0*0.5*70 + 1.25*1.5*80) / (2.0*0.5 + 1.25*1.5)
    expected = (2.0 * 0.5 * 70.0 + 1.25 * 1.5 * 80.0) / (2.0 * 0.5 + 1.25 * 1.5)
    assert skewed == pytest.approx(expected, abs=0.05)


def test_blend_unknown_model_in_weights_is_ignored():
    """משקל למודל שאין לו תחזית היום — לא מפיל ולא משנה כלום."""
    base = blended_forecast_high(_signals())
    same = blended_forecast_high(_signals(weights={"icon_forecast": 1.4}))
    assert base == same
