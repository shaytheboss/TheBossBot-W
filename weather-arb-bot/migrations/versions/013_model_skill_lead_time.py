"""הוספת עמודת days_ahead לטבלת model_skill — מדידת דיוק פר-זמן-הקדמה.

עד עכשיו כל שורה ייצגה (עיר, מודל) בלי קשר למתי פורסמה התחזית.
עכשיו מוסיפים days_ahead (כמה ימים לפני יום האירוע): תחזית שניתנה
יומיים מראש תיבדק מול accuracy-יומיים, ולא תתערבב עם אותו-יום.

ה-unique constraint משתנה: (city_id, source) → (city_id, source, days_ahead).
כל השורות הקיימות מקבלות days_ahead=0 (ה"מילה האחרונה" הישנה).

Revision ID: 013_model_skill_lead_time
Revises: 012_model_skill
"""
import sqlalchemy as sa
from alembic import op

revision = "013_model_skill_lead_time"
down_revision = "012_model_skill"
branch_labels = None
depends_on = None


def upgrade():
    # מוסיפים את העמודה (default=0 כדי שהשורות הקיימות יקבלו days_ahead=0)
    op.add_column(
        "model_skill",
        sa.Column("days_ahead", sa.Integer(), nullable=False, server_default="0"),
    )
    # מחליפים את ה-unique constraint הישן
    op.drop_constraint("uq_model_skill_city_source", "model_skill", type_="unique")
    op.create_unique_constraint(
        "uq_model_skill_city_source_da",
        "model_skill",
        ["city_id", "source", "days_ahead"],
    )


def downgrade():
    op.drop_constraint("uq_model_skill_city_source_da", "model_skill", type_="unique")
    op.drop_column("model_skill", "days_ahead")
    op.create_unique_constraint(
        "uq_model_skill_city_source",
        "model_skill",
        ["city_id", "source"],
    )
