from sqlalchemy import (
    Column, Date, Float, ForeignKey, Integer, String, TIMESTAMP, UniqueConstraint,
)
from sqlalchemy.sql import func

from app.database import Base


class ModelSkill(Base):
    """מאגר מנוהל של דיוק מודלים פר-עיר ופר-זמן-הקדמה — "מי באמת צודק כאן".

    שורה אחת לכל (עיר, מודל, days_ahead). days_ahead הוא כמה ימים לפני
    יום האירוע פורסמה התחזית שנמדדת: 0 = ביום עצמו, 1 = יום לפני, 2 = יומיים לפני.
    כך מודל שמצטיין בתחזית יומיים מראש נחשב בנפרד מביצועיו ביום האירוע עצמו —
    ומשקל ה-2-day-ahead שלו ייחשב רק כשנדע כמה הקדמה יש בהתראה הנוכחית.

    הציון נמדד מול האמת היחידה שמשלמת: הדלי שפולימרקט סגרה כמנצח.
    לא מול METAR, לא מול תחזיות אחרות — מול התוצאה שעליה מתחשבן הכסף.

    גם הבוט היומי וגם התוך-יומי קוראים מכאן:
      - היומי: משקל בממוצע הדטרמיניסטי (probability_estimator)
      - התוך-יומי: מכפיל על המשקל הבסיסי בבלנד התחזיות (detector)
    """
    __tablename__ = "model_skill"
    __table_args__ = (
        UniqueConstraint("city_id", "source", "days_ahead",
                         name="uq_model_skill_city_source_da"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False, index=True)
    source = Column(String(32), nullable=False)        # 'gfs' / 'ecmwf' / 'hrrr' / ...
    # כמה ימים לפני יום האירוע פורסמה התחזית הנמדדת:
    #   0 = ביום האירוע עצמו (ה"מילה האחרונה"), 1 = יום לפני, 2 = יומיים לפני, ...
    days_ahead = Column(Integer, nullable=False, default=0)

    # ── הסטטיסטיקה עצמה (על חלון הזמן האחרון בלבד) ──
    samples = Column(Integer, nullable=False, default=0)   # שווקים סגורים שנמדדו
    hits = Column(Integer, nullable=False, default=0)      # התחזית נחתה בדלי המנצח
    hit_rate = Column(Float, nullable=True)                # hits/samples (החלקת לפלס)
    mae_f = Column(Float, nullable=True)                   # מרחק ממוצע (°F) מהדלי המנצח; 0=בפנים
    bias_f = Column(Float, nullable=True)                  # שגיאה חתומה ממוצעת (חיובי=המודל חוזה גבוה מדי)

    # המשקל הסופי שהחיזוי משתמש בו: 1.0=ניטרלי, טווח קשיח [0.5, 1.5].
    # מודל בלי מספיק דגימות נשאר ניטרלי — אין ענישה על חוסר היסטוריה.
    weight = Column(Float, nullable=False, default=1.0)

    window_days = Column(Integer, nullable=False, default=90)   # אורך חלון המדידה
    last_event_date = Column(Date, nullable=True)               # האירוע האחרון שנכלל
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(),
                        onupdate=func.now())
