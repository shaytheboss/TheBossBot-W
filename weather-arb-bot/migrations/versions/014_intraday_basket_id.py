"""הוספת basket_id לטבלת intraday_opportunities — תיוג רגלי-סל.

כאשר הבוט מזהה שניתן לקנות NO על 3+ דליים של אותו שוק עם EV חיובי כולל,
כל הרגליים מקבלות basket_id משותף לצורך מעקב ביצועים ייעודי.
פורמט: "bkt_YYYYMMDD_HHMMSS_<market_id>".

Revision ID: 014_intraday_basket_id
Revises: 013_model_skill_lead_time
"""
import sqlalchemy as sa
from alembic import op

revision = "014_intraday_basket_id"
down_revision = "013_model_skill_lead_time"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "intraday_opportunities",
        sa.Column("basket_id", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_intraday_opportunities_basket_id",
        "intraday_opportunities",
        ["basket_id"],
    )


def downgrade():
    op.drop_index("ix_intraday_opportunities_basket_id", table_name="intraday_opportunities")
    op.drop_column("intraday_opportunities", "basket_id")
