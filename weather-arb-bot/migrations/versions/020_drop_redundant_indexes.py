"""Drop redundant duplicate indexes on market_prices and metar_observations.

Both tables declare a UniqueConstraint AND a plain Index over the SAME columns:

    market_prices:       UNIQUE (outcome_id, timestamp)
                       + INDEX idx_prices_outcome_time (outcome_id, timestamp)
    metar_observations:  UNIQUE (icao, observed_at)
                       + INDEX idx_metar_icao_time (icao, observed_at)

A UniqueConstraint is implemented as a unique btree index, which already serves
every lookup, range scan and ORDER BY the plain index would serve. The second
index is therefore pure overhead: extra disk, extra RAM in cache, and extra work
on every INSERT.

Measured impact: market_prices holds 17.5M rows with 1,819 MB of indexes; the
redundant copy accounts for roughly 600-700 MB of a 3,871 MB database — freed
with zero data loss and no query-plan regression.

Revision ID: 020_drop_redundant_indexes
Revises: 019_app_settings
"""
from alembic import op

revision = "020_drop_redundant_indexes"
down_revision = "019_app_settings"
branch_labels = None
depends_on = None


def upgrade():
    # IF EXISTS: these are duplicates, so a missing one is not an error.
    op.execute("DROP INDEX IF EXISTS idx_prices_outcome_time")
    op.execute("DROP INDEX IF EXISTS idx_metar_icao_time")


def downgrade():
    op.create_index(
        "idx_prices_outcome_time", "market_prices", ["outcome_id", "timestamp"]
    )
    op.create_index(
        "idx_metar_icao_time", "metar_observations", ["icao", "observed_at"]
    )
