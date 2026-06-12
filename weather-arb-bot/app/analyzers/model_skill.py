"""מאגר מנוהל של דיוק מודלים פר-עיר — הלב של "מי באמת צודק כאן".

הרעיון: לכל (עיר, מודל) נמדוד שוב ושוב כמה התחזית של המודל קלעה לדלי
שפולימרקט סגרה בפועל כמנצח — האמת היחידה שמשלמת. את התוצאה נשמור
בטבלה מנוהלת (model_skill) שמתעדכנת אחרי כל settlement וב-job תקופתי,
כך שהמשקולות נושמות עם הזמן: מודל שמצטיין בעיר מסוימת מקבל משקל גבוה,
מודל שדועך מאבד אותו מעצמו (חלון מדידה מתגלגל של WINDOW_DAYS).

מי צורך את המשקולות:
  - הבוט היומי:    probability_estimator משקלל את הממוצע הדטרמיניסטי
  - הבוט התוך-יומי: detector מכפיל את משקלי הבסיס בבלנד התחזיות
  - הדשבורד:        מסך "Model skill" מציג את הטבלה החיה

מקור הנתונים: טבלת forecasts הגולמית (לא Opportunity.signals!) — לכל
שוק סגור נלקח הסנפשוט האחרון שכל מודל פרסם ביום האירוע עצמו. זה מכסה
אוטומטית גם את היומי וגם את התוך-יומי, כי שניהם ניזונים מאותן תחזיות.

עקרון בטיחות: מודל בלי מספיק דגימות (MIN_SAMPLES) נשאר במשקל ניטרלי
1.0 — אין ענישה על חוסר היסטוריה, וההתנהגות זהה לממוצע לא-משוקלל עד
שמצטברות ראיות. המשקל כלוא קשיח ב-[0.5, 1.5] כדי שאף מודל לא ישתלט.
"""
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.city import City
from app.models.forecast import Forecast
from app.models.market import Market, MarketOutcome
from app.models.model_skill import ModelSkill
from app.utils.units import resolve_bucket_unit

logger = logging.getLogger(__name__)

# כמה שווקים סגורים נדרשים לפני שמודל מקבל משקל לא-ניטרלי
MIN_SAMPLES = 5
# חלון המדידה המתגלגל — מעבר לזה ההיסטוריה פשוט נשכחת
WINDOW_DAYS = 90
# TTL לקאש הקריאה (החיזוי רץ כל כמה דקות; המשקולות זזות בקצב יומי)
CACHE_TTL_SECONDS = 300

# המודלים שנמדדים. Wunderground בכוונה לא כאן — ביום האירוע ה"תחזית"
# שלו היא התצפית-עד-כה (עמוד ההיסטוריה), לא חיזוי, ולכן ציון עליו
# היה מנופח ושקרי.
SKILL_SOURCES: tuple[str, ...] = (
    "gfs", "ecmwf", "hrrr", "nws", "tomorrowio", "meteosource", "icon",
)

# מיפוי שם-מקור ↔ מפתח-סיגנלים (כפי שהאסטימטורים מכירים אותו)
SOURCE_TO_SIGNAL_KEY = {s: f"{s}_forecast" for s in SKILL_SOURCES}

_cache: dict[int, tuple[float, dict]] = {}


def invalidate_cache() -> None:
    """ניקוי הקאש — נקרא אחרי כל עדכון של הטבלה כדי שהחיזוי הבא
    יראה מיד את המשקולות הטריות."""
    _cache.clear()


# ── הניקוד עצמו (פונקציות טהורות, בדיקות-יחידה ישירות) ─────────────────────

def _bucket_f_interval(unit: str, bmin, bmax) -> tuple[Optional[float], Optional[float]]:
    """גבולות הדלי כקטע חצי-פתוח [lo, hi) במעלות פרנהייט.

    הסמנטיקה זהה ל-temp_in_bucket: דלי [bmin, bmax] מכסה [bmin, bmax+1)
    ביחידה הילידית; בצלזיוס ממירים את הקצוות ל-°F (המרה לינארית שומרת
    על סדר, אז הקטע נשאר קטע).
    """
    lo = float(bmin) if bmin is not None else None
    hi = float(bmax) + 1.0 if bmax is not None else None
    if unit == "C":
        lo = lo * 9.0 / 5.0 + 32.0 if lo is not None else None
        hi = hi * 9.0 / 5.0 + 32.0 if hi is not None else None
    return lo, hi


def score_forecast(forecast_f: float, winners: list) -> Optional[tuple[bool, float, float]]:
    """ציון תחזית אחת מול הדלי(ים) שפולימרקט סגרה כמנצחים.

    מחזיר (hit, distance_f, signed_err_f):
      hit         — התחזית נחתה בתוך דלי מנצח
      distance_f  — המרחק (°F) לדלי המנצח הקרוב; 0 כשבפנים
      signed_err  — חתום: חיובי = המודל חזה גבוה מדי, שלילי = נמוך מדי
    None כשאין מנצחים ידועים (שוק לא באמת סגור אצלנו).
    """
    if not winners:
        return None
    best: Optional[tuple[float, float]] = None   # (distance, signed)
    for unit, bmin, bmax in winners:
        lo, hi = _bucket_f_interval(unit, bmin, bmax)
        if lo is not None and forecast_f < lo:
            cand = (lo - forecast_f, forecast_f - lo)        # מתחת לרצפה → שלילי
        elif hi is not None and forecast_f >= hi:
            cand = (forecast_f - hi, forecast_f - hi)        # מעל התקרה → חיובי
        else:
            cand = (0.0, 0.0)                                # בפנים — פגיעה
        if best is None or cand[0] < best[0]:
            best = cand
    dist, signed = best
    return (dist == 0.0, dist, signed)


def skill_weight(hits: int, samples: int) -> float:
    """המשקל הסופי מהסטטיסטיקה — אותה נוסחה כמו המנגנון הוותיק כדי
    שהמעבר לטבלה לא ישנה את התנהגות החיזוי כהוא-זה:

        weight = 0.5 + (hits+1)/(samples+2)      ∈ [0.5, 1.5]

    החלקת לפלס מצמידה דגימות קטנות ל-1.0 (ניטרלי); מתחת ל-MIN_SAMPLES
    מוחזר 1.0 במפורש.
    """
    if samples < MIN_SAMPLES:
        return 1.0
    return round(0.5 + (hits + 1.0) / (samples + 2.0), 3)


# ── חישוב ועדכון הטבלה ──────────────────────────────────────────────────────

async def compute_city_skill(db: AsyncSession, city_id: int) -> dict[str, dict]:
    """סטטיסטיקת דיוק לכל מודל בעיר אחת, על חלון הזמן המתגלגל.

    מחזיר {source: {samples, hits, dist_sum, signed_sum, last_event}}.
    לכל שוק סגור נלקח הסנפשוט האחרון שהמודל פרסם ביום האירוע עצמו —
    "המילה האחרונה" של המודל לפני שהיום הוכרע.
    """
    since = date.today() - timedelta(days=WINDOW_DAYS)

    # 1. כל השווקים הסגורים של העיר בחלון, עם הדליים המנצחים שלהם
    markets = (await db.execute(
        select(Market.id, Market.event_date).where(
            Market.city_id == city_id,
            Market.resolved == True,
            Market.event_date >= since,
        )
    )).all()
    if not markets:
        return {}
    market_dates = {mid: ev for mid, ev in markets}

    won_rows = (await db.execute(
        select(MarketOutcome).where(
            MarketOutcome.market_id.in_(market_dates.keys()),
            MarketOutcome.won == True,
        )
    )).scalars().all()
    winners_by_market: dict[int, list] = {}
    for oc in won_rows:
        winners_by_market.setdefault(oc.market_id, []).append(
            (resolve_bucket_unit(oc), oc.bucket_min, oc.bucket_max)
        )
    if not winners_by_market:
        return {}

    # 2. "המילה האחרונה" של כל מודל לכל יום-אירוע: התחזית עם retrieved_at
    #    המאוחר ביותר עבור forecast_for_date == יום האירוע.
    event_days = sorted({market_dates[mid] for mid in winners_by_market})
    sub = (
        select(
            Forecast.source,
            Forecast.forecast_for_date,
            sqlfunc.max(Forecast.retrieved_at).label("last_ts"),
        )
        .where(
            Forecast.city_id == city_id,
            Forecast.source.in_(SKILL_SOURCES),
            Forecast.forecast_for_date.in_(event_days),
        )
        .group_by(Forecast.source, Forecast.forecast_for_date)
        .subquery()
    )
    fc_rows = (await db.execute(
        select(Forecast).join(
            sub,
            (Forecast.source == sub.c.source)
            & (Forecast.forecast_for_date == sub.c.forecast_for_date)
            & (Forecast.retrieved_at == sub.c.last_ts),
        ).where(Forecast.city_id == city_id)
    )).scalars().all()
    # (source, event_date) → predicted_high_f
    final_word: dict[tuple, float] = {}
    for fc in fc_rows:
        if fc.predicted_high_f is not None:
            final_word[(fc.source, fc.forecast_for_date)] = float(fc.predicted_high_f)

    # 3. צבירה: לכל שוק סגור, ציון כל מודל שפרסם תחזית ליום הזה
    stats: dict[str, dict] = {}
    for mid, winners in winners_by_market.items():
        ev = market_dates[mid]
        for source in SKILL_SOURCES:
            fc_val = final_word.get((source, ev))
            if fc_val is None:
                continue
            scored = score_forecast(fc_val, winners)
            if scored is None:
                continue
            hit, dist, signed = scored
            s = stats.setdefault(source, {
                "samples": 0, "hits": 0, "dist_sum": 0.0,
                "signed_sum": 0.0, "last_event": None,
            })
            s["samples"] += 1
            s["hits"] += 1 if hit else 0
            s["dist_sum"] += dist
            s["signed_sum"] += signed
            if s["last_event"] is None or ev > s["last_event"]:
                s["last_event"] = ev
    return stats


async def update_model_skill(db: AsyncSession) -> dict:
    """עדכון הטבלה לכל הערים הפעילות (upsert). מחזיר סיכום ללוג/API.

    נקרא משני מקומות: ה-job התקופתי, ומיד אחרי שכל settlement מסתיים —
    כדי שהמשקולות יתעדכנו באותו יום שבו נולדה התוצאה ולא יחכו לסבב הבא.
    """
    cities = (await db.execute(
        select(City).where(City.active == True)
    )).scalars().all()

    updated_rows = 0
    for city in cities:
        try:
            stats = await compute_city_skill(db, city.id)
        except Exception as exc:
            logger.warning("model_skill: compute failed for city %s: %s", city.id, exc)
            continue

        # שורות קיימות של העיר — upsert ידני (עובד גם על SQLite בבדיקות)
        existing = {
            row.source: row
            for row in (await db.execute(
                select(ModelSkill).where(ModelSkill.city_id == city.id)
            )).scalars().all()
        }
        for source, s in stats.items():
            n, h = s["samples"], s["hits"]
            row = existing.get(source)
            if row is None:
                row = ModelSkill(city_id=city.id, source=source)
                db.add(row)
            row.samples = n
            row.hits = h
            # ה-hit_rate המוצג הוא המוחלק (אותו ערך שמזין את המשקל)
            row.hit_rate = round((h + 1.0) / (n + 2.0), 4)
            row.mae_f = round(s["dist_sum"] / n, 2) if n else None
            row.bias_f = round(s["signed_sum"] / n, 2) if n else None
            row.weight = skill_weight(h, n)
            row.window_days = WINDOW_DAYS
            row.last_event_date = s["last_event"]
            row.updated_at = datetime.now(timezone.utc)
            updated_rows += 1
        # מודל שנעלם מהחלון (אין לו עוד שווקים) — מתאפס לניטרלי במקום
        # לגרור משקל ישן לנצח
        for source, row in existing.items():
            if source not in stats:
                row.samples = 0
                row.hits = 0
                row.hit_rate = None
                row.mae_f = None
                row.bias_f = None
                row.weight = 1.0
                row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    invalidate_cache()   # שהחיזוי הבא יראה את המשקולות הטריות מיד
    summary = {"cities": len(cities), "rows_updated": updated_rows}
    logger.info("model_skill updated: %s", summary)
    return summary


# ── הקריאה מצד החיזוי ───────────────────────────────────────────────────────

async def get_skill_weights(db: AsyncSession, city_id: int) -> dict:
    """משקולות לעיר במפתחות-סיגנלים: {'gfs_forecast': 1.32, ...}.

    קודם מהטבלה המנוהלת (עם קאש קצר). אם לטבלה אין עדיין שורות לעיר
    (לפני הריצה הראשונה של ה-job) — נפילה רכה למנגנון החישוב הוותיק,
    כדי שהמעבר לא ישאיר אף עיר בלי משקולות אפילו לרגע.
    """
    now = time.monotonic()
    cached = _cache.get(city_id)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    weights: dict = {}
    try:
        rows = (await db.execute(
            select(ModelSkill).where(ModelSkill.city_id == city_id)
        )).scalars().all()
        for row in rows:
            if row.samples >= MIN_SAMPLES:
                key = SOURCE_TO_SIGNAL_KEY.get(row.source)
                if key:
                    weights[key] = float(row.weight)
    except Exception as exc:
        logger.warning("model_skill: read failed for city %s: %s", city_id, exc)

    if not weights:
        # נפילה רכה: החישוב הוותיק (מ-Opportunity.signals) עד שהטבלה תתמלא
        try:
            from app.analyzers.model_weights import get_city_model_weights
            weights = await get_city_model_weights(db, city_id)
        except Exception as exc:
            logger.warning("model_skill: legacy fallback failed for city %s: %s",
                           city_id, exc)
            weights = {}

    _cache[city_id] = (now, weights)
    return weights
