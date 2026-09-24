"""Widen intraday_opportunities.lock_state from VARCHAR(20) to VARCHAR(32).

The estimator can emit "yes_impossible_unconfirmed" (26 characters), and now
"yes_impossible_marginal" (23). At VARCHAR(20) Postgres rejects the insert with
"value too long for type character varying(20)". The detector's per-outcome
handler logs and continues without rolling back, so after that failed flush
every remaining insert in the same session failed too — the rest of that
intraday cycle was silently lost. SQLite ignores VARCHAR lengths, which is why
no test ever saw it.

Increasing a VARCHAR limit is a catalog-only change in Postgres: no table
rewrite, no data touched.

Revision ID: 024_widen_intraday_lock_state
Revises: 023_shadow_live_prices
"""
from alembic import op
import sqlalchemy as sa

revision = "024_widen_intraday_lock_state"
down_revision = "023_shadow_live_prices"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "intraday_opportunities", "lock_state",
        type_=sa.String(32), existing_type=sa.String(20), existing_nullable=True,
    )


def downgrade():
    # Values longer than 20 would not fit back; truncate rather than fail.
    op.execute(
        "UPDATE intraday_opportunities SET lock_state = LEFT(lock_state, 20) "
        "WHERE length(lock_state) > 20"
    )
    op.alter_column(
        "intraday_opportunities", "lock_state",
        type_=sa.String(20), existing_type=sa.String(32), existing_nullable=True,
    )
