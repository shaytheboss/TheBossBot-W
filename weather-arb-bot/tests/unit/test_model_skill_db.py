"""בדיקת אינטגרציה למאגר דיוק-המודלים — מסלול מלא מול DB אמיתי (SQLite בזיכרון).

מדמים עיר עם שווקים סגורים של פולימרקט ותחזיות גולמיות, מריצים את
update_model_skill, ובודקים שהטבלה המנוהלת מתמלאת נכון ושמשקולות
הקריאה (get_skill_weights) הן בדיוק מה שהחיזוי אמור לראות:
- מודל מדויק → משקל גבוה; מודל גרוע → משקל נמוך
- מעט דגימות → ניטרלי (לא מוחזר בכלל)
- עדכון חוזר הוא upsert (לא שורות כפולות)
- days_ahead מופרד — תחזית יומיים מראש לא מתערבבת עם אותו-יום
- מודל שנעלם מהחלון מתאפס לניטרלי
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import select

from app.database import Base
from app.models import City, Forecast, Market, MarketOutcome, ModelSkill
import app.analyzers.model_skill as ms


# ‏SQLite לא מכיר JSONB של פוסטגרס — בבדיקות מרנדרים אותו כ-JSON רגיל.
@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


# ‏SQLite נותן autoincrement רק ל-INTEGER PRIMARY KEY — ‏BIGINT לא מקבל
# rowid אוטומטי, אז מרנדרים אותו כ-INTEGER בבדיקות בלבד.
from sqlalchemy import BigInteger  # noqa: E402


@compiles(BigInteger, "sqlite")
def _bigint_as_int(type_, compiler, **kw):
    return "INTEGER"


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [m.__table__ for m in (City, Market, MarketOutcome, Forecast, ModelSkill)]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_city(db) -> City:
    city = City(
        name="Testville", primary_icao="KTST", timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/KTST",
        active=True,
    )
    db.add(city)
    await db.flush()
    return city


async def _seed_resolved_market(db, city, event_date, winning_range):
    """שוק סגור אחד עם דלי מנצח אחד (winning_range=(bmin,bmax) ב-°F)."""
    market = Market(
        city_id=city.id, external_id=f"ev-{event_date}", platform="polymarket",
        question="q", event_date=event_date, resolved=True,
    )
    db.add(market)
    await db.flush()
    win = MarketOutcome(
        market_id=market.id, bucket_label=f"{winning_range[0]}-{winning_range[1]}",
        bucket_min=winning_range[0], bucket_max=winning_range[1],
        bucket_unit="F", won=True,
    )
    lose = MarketOutcome(
        market_id=market.id, bucket_label="loser",
        bucket_min=winning_range[0] - 10, bucket_max=winning_range[1] - 10,
        bucket_unit="F", won=False,
    )
    db.add_all([win, lose])
    await db.flush()
    return market


async def _seed_forecast(db, city, source, event_date, high_f, hours_ago=2):
    """שתול תחזית.

    hours_ago = שעות לפני חצות שאחרי יום האירוע, כך שברירת-מחדל (2h) = 22:00
    ביום האירוע עצמו → days_ahead=0.
    ערכים גדולים יותר: hours_ago=26 = 22:00 יום לפני → days_ahead=1.
    """
    midnight_after = datetime(
        event_date.year, event_date.month, event_date.day,
        tzinfo=timezone.utc,
    ) + timedelta(days=1)
    db.add(Forecast(
        city_id=city.id, source=source, forecast_for_date=event_date,
        predicted_high_f=high_f,
        retrieved_at=midnight_after - timedelta(hours=hours_ago),
    ))
    await db.flush()


async def _seed_forecast_days_ahead(db, city, source, event_date, high_f, days_ahead):
    """שתול תחזית שפורסמה days_ahead ימים לפני האירוע (בצהריים של אותו יום)."""
    retrieved_date = event_date - timedelta(days=days_ahead)
    retrieved = datetime(
        retrieved_date.year, retrieved_date.month, retrieved_date.day,
        12, 0, 0, tzinfo=timezone.utc,
    )
    db.add(Forecast(
        city_id=city.id, source=source, forecast_for_date=event_date,
        predicted_high_f=high_f,
        retrieved_at=retrieved,
    ))
    await db.flush()


async def test_full_skill_pipeline(db):
    """‏GFS קולע 6/6, ‏ECMWF מפספס 6/6 — המשקולות חייבות לשקף את זה."""
    city = await _seed_city(db)
    for i in range(6):
        ev = date.today() - timedelta(days=i + 1)
        await _seed_resolved_market(db, city, ev, (76, 77))   # מנצח: [76, 78)
        await _seed_forecast(db, city, "gfs", ev, 76.5)        # בפנים — פגיעה
        await _seed_forecast(db, city, "ecmwf", ev, 81.0)      # ‎+3 מעל — פספוס
    await db.commit()

    summary = await ms.update_model_skill(db)
    assert summary["rows_updated"] == 2   # gfs ו-ecmwf, שניהם days_ahead=0

    rows = {(r.source, r.days_ahead): r
            for r in (await db.execute(select(ModelSkill))).scalars().all()}
    gfs, ec = rows[("gfs", 0)], rows[("ecmwf", 0)]
    assert gfs.samples == 6 and gfs.hits == 6
    assert ec.samples == 6 and ec.hits == 0
    assert gfs.weight > 1.2 and ec.weight < 0.8
    assert gfs.mae_f == 0.0
    assert ec.mae_f == pytest.approx(3.0)
    assert ec.bias_f == pytest.approx(3.0)     # חיובי = חוזה גבוה מדי

    # הקריאה מצד החיזוי — במפתחות-סיגנלים, רק מודלים עם מספיק דגימות
    ms.invalidate_cache()
    weights = await ms.get_skill_weights(db, city.id, days_ahead=0)
    assert weights["gfs_forecast"] == gfs.weight
    assert weights["ecmwf_forecast"] == ec.weight


async def test_few_samples_stay_neutral(db):
    """פחות מ-MIN_SAMPLES שווקים → המודל לא מקבל משקל (ניטרלי בחיזוי)."""
    city = await _seed_city(db)
    for i in range(ms.MIN_SAMPLES - 1):
        ev = date.today() - timedelta(days=i + 1)
        await _seed_resolved_market(db, city, ev, (76, 77))
        await _seed_forecast(db, city, "nws", ev, 76.5)
    await db.commit()

    await ms.update_model_skill(db)
    row = (await db.execute(select(ModelSkill))).scalars().one()
    assert row.samples == ms.MIN_SAMPLES - 1
    assert row.weight == 1.0                       # נשמר בטבלה — אבל ניטרלי
    ms.invalidate_cache()
    weights = await ms.get_skill_weights(db, city.id, days_ahead=0)
    assert "nws_forecast" not in weights           # החיזוי לא רואה אותו בכלל


async def test_update_is_upsert_not_duplicate(db):
    """ריצה כפולה לא יוצרת שורות כפולות — עדכון במקום."""
    city = await _seed_city(db)
    for i in range(6):
        ev = date.today() - timedelta(days=i + 1)
        await _seed_resolved_market(db, city, ev, (76, 77))
        await _seed_forecast(db, city, "gfs", ev, 76.5)
    await db.commit()

    await ms.update_model_skill(db)
    await ms.update_model_skill(db)
    rows = (await db.execute(select(ModelSkill))).scalars().all()
    assert len(rows) == 1   # רק (gfs, days_ahead=0)


async def test_last_forecast_of_event_day_wins(db):
    """‏"המילה האחרונה": כשמודל פרסם כמה תחזיות לאותו יום, נמדדת המאוחרת."""
    city = await _seed_city(db)
    ev = date.today() - timedelta(days=1)
    for i in range(6):
        d = date.today() - timedelta(days=i + 1)
        await _seed_resolved_market(db, city, d, (76, 77))
        if d != ev:
            await _seed_forecast(db, city, "gfs", d, 76.5)
    # ליום ev: תחזית מוקדמת גרועה (85) ומאוחרת מדויקת (76.5)
    await _seed_forecast(db, city, "gfs", ev, 85.0, hours_ago=10)
    await _seed_forecast(db, city, "gfs", ev, 76.5, hours_ago=1)
    await db.commit()

    await ms.update_model_skill(db)
    row = (await db.execute(select(ModelSkill))).scalars().one()
    assert row.hits == 6                            # גם ev נספר כפגיעה


async def test_celsius_market_scored_correctly(db):
    """שוק צלזיוס: דלי 25°C מנצח = [77.0, 78.8)°F — תחזית 77.5 פוגעת."""
    city = await _seed_city(db)
    for i in range(6):
        ev = date.today() - timedelta(days=i + 1)
        market = Market(
            city_id=city.id, external_id=f"c-{ev}", platform="polymarket",
            question="q", event_date=ev, resolved=True,
        )
        db.add(market)
        await db.flush()
        db.add(MarketOutcome(
            market_id=market.id, bucket_label="25°C",
            bucket_min=25, bucket_max=25, bucket_unit="C", won=True,
        ))
        await db.flush()
        await _seed_forecast(db, city, "hrrr", ev, 77.5)
    await db.commit()

    await ms.update_model_skill(db)
    row = (await db.execute(select(ModelSkill))).scalars().one()
    assert row.source == "hrrr"
    assert row.hits == 6


async def test_model_dropping_out_of_window_resets_to_neutral(db):
    """מודל שכל השווקים שלו יצאו מהחלון מתאפס לניטרלי — לא גורר עבר."""
    city = await _seed_city(db)
    # שותלים ידנית שורה ישנה עם משקל גבוה
    db.add(ModelSkill(
        city_id=city.id, source="icon", days_ahead=0, samples=20, hits=18,
        hit_rate=0.86, weight=1.36,
    ))
    await db.commit()

    await ms.update_model_skill(db)   # אין שווקים בחלון → איפוס
    row = (await db.execute(select(ModelSkill))).scalars().one()
    assert row.weight == 1.0
    assert row.samples == 0


async def test_lead_time_tracked_separately(db):
    """תחזיות ב-days_ahead שונים נמדדות בנפרד — ולא מתערבבות זו עם זו."""
    city = await _seed_city(db)
    for i in range(6):
        ev = date.today() - timedelta(days=i + 1)
        await _seed_resolved_market(db, city, ev, (76, 77))   # מנצח: [76, 78)
        # same-day (days_ahead=0): GFS פוגע
        await _seed_forecast_days_ahead(db, city, "gfs", ev, 76.5, days_ahead=0)
        # 2-day-ahead (days_ahead=2): GFS מפספס
        await _seed_forecast_days_ahead(db, city, "gfs", ev, 83.0, days_ahead=2)
    await db.commit()

    await ms.update_model_skill(db)

    rows = {(r.source, r.days_ahead): r
            for r in (await db.execute(select(ModelSkill))).scalars().all()}

    # same-day: כל הפגיעות
    assert ("gfs", 0) in rows
    assert rows[("gfs", 0)].hits == 6

    # 2-day-ahead: כל הפספוסים — שורה נפרדת
    assert ("gfs", 2) in rows
    assert rows[("gfs", 2)].hits == 0
    assert rows[("gfs", 2)].weight < 0.8

    # get_skill_weights עם days_ahead=0 מחזיר את הטוב
    ms.invalidate_cache()
    w0 = await ms.get_skill_weights(db, city.id, days_ahead=0)
    assert w0["gfs_forecast"] > 1.2

    # get_skill_weights עם days_ahead=2 מחזיר את הגרוע
    ms.invalidate_cache()
    w2 = await ms.get_skill_weights(db, city.id, days_ahead=2)
    assert w2["gfs_forecast"] < 0.8


async def test_days_ahead_cache_key_is_independent(db):
    """הקאש עבור days_ahead=0 ו-days_ahead=2 הם עצמאיים — לא מחזירים אחד את השני."""
    city = await _seed_city(db)
    for i in range(6):
        ev = date.today() - timedelta(days=i + 1)
        await _seed_resolved_market(db, city, ev, (76, 77))
        await _seed_forecast_days_ahead(db, city, "gfs", ev, 76.5, days_ahead=0)
        await _seed_forecast_days_ahead(db, city, "gfs", ev, 83.0, days_ahead=1)
    await db.commit()
    await ms.update_model_skill(db)
    ms.invalidate_cache()

    w0 = await ms.get_skill_weights(db, city.id, days_ahead=0)
    w1 = await ms.get_skill_weights(db, city.id, days_ahead=1)
    # שני days_ahead שונים → משקולות שונות
    assert w0.get("gfs_forecast") != w1.get("gfs_forecast")
