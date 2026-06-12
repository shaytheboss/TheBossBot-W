"""מאגר דיוק מודלים פר-עיר (model_skill).

שורה לכל (עיר, מודל): כמה פעמים התחזית של המודל נחתה בדלי שפולימרקט
סגרה כמנצח, מרחק ממוצע, הטיה חתומה — והמשקל הסופי [0.5, 1.5] שהחיזוי
(היומי והתוך-יומי) משתמש בו. מתעדכן אחרי כל settlement וב-job תקופתי.

Revision ID: 012_model_skill
Revises: 011_intraday
"""
import sqlalchemy as sa
from alembic import op

revision = "012_model_skill"
down_revision = "011_intraday"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_skill",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hit_rate", sa.Float(), nullable=True),
        sa.Column("mae_f", sa.Float(), nullable=True),
        sa.Column("bias_f", sa.Float(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("last_event_date", sa.Date(), nullable=True),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("city_id", "source", name="uq_model_skill_city_source"),
    )
    op.create_index("ix_model_skill_city_id", "model_skill", ["city_id"])


def downgrade():
    op.drop_index("ix_model_skill_city_id", table_name="model_skill")
    op.drop_table("model_skill")
