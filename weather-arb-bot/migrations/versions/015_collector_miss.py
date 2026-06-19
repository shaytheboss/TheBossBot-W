"""יצירת טבלת collector_miss — מעקב אחר מקורות מידע חסרים פר-עיר ופר-תאריך.

בכל פעם שהמעריך מגלה שמקור גלובלי (ECMWF, GFS, Tomorrow.io וכו') לא החזיר
נתונים לעיר מסוימת, נרשמת שורה בטבלה. ניתוח מצטבר של הטבלה חושף פערים
שיטתיים: "ECMWF אף פעם לא עונה עבור טוקיו" / "Tomorrow.io לא מוגדר".

Revision ID: 015_collector_miss
Revises: 014_intraday_basket_id
"""
import sqlalchemy as sa
from alembic import op

revision = "015_collector_miss"
down_revision = "014_intraday_basket_id"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collector_miss",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("miss_reason", sa.String(16), nullable=False, server_default="no_data"),
        sa.Column(
            "detected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_collector_miss_city_date",
        "collector_miss",
        ["city_id", "event_date"],
    )
    op.create_unique_constraint(
        "uq_collector_miss_city_date_source",
        "collector_miss",
        ["city_id", "event_date", "source", "miss_reason"],
    )


def downgrade():
    op.drop_table("collector_miss")
