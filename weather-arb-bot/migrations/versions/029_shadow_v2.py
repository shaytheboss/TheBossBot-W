"""Shadow study: record model v2's probability and mean next to the current model.

Two nullable REAL columns on shadow_snapshots — catalog-only in Postgres,
no rewrite of existing rows.

Revision ID: 029_shadow_v2
Revises: 028_fix_last_years_markets
"""
from alembic import op
import sqlalchemy as sa

revision = "029_shadow_v2"
down_revision = "028_fix_last_years_markets"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("shadow_snapshots", sa.Column("v2_p", sa.REAL(), nullable=True))
    op.add_column("shadow_snapshots", sa.Column("v2_mu", sa.REAL(), nullable=True))


def downgrade():
    op.drop_column("shadow_snapshots", "v2_mu")
    op.drop_column("shadow_snapshots", "v2_p")
