"""Add the missing indexes behind the admin screens.

`opportunities` carried NO index at all beyond its primary key — yet every
Positions / Opportunities / Stats query does:

    WHERE virtual_shares IS NOT NULL          -- positions only
      AND detected_at BETWEEN ... AND ...
    ORDER BY detected_at DESC

With no index Postgres sequentially scans the whole table and sorts the result
on EVERY request. The table is ~55 MB (12.8k rows, each carrying a multi-KB
`signals` JSONB), so each screen load read the entire table from disk, pushed it
through shared_buffers and sorted it — which is what made the admin screens time
out with "TypeError: Failed to fetch", and a standing CPU/RAM cost that grows
with the table.

Indexes added:
  ix_opportunities_detected_at            — the Opportunities screen + date range
  ix_opportunities_positions (partial)    — the Positions screen; the partial
                                            predicate keeps it tiny because only
                                            a fraction of rows are virtual buys
  ix_alerts_sent_at                       — alert history / pruning
  ix_intraday_detected_at                 — the Intraday screens

All are plain btree creations: no data change, fully reversible.

Revision ID: 021_admin_query_indexes
Revises: 020_drop_redundant_indexes
"""
from alembic import op
import sqlalchemy as sa

revision = "021_admin_query_indexes"
down_revision = "020_drop_redundant_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_opportunities_detected_at "
        "ON opportunities (detected_at DESC)"
    )
    # Partial index: the Positions screen only ever looks at virtual buys, so
    # restricting the predicate keeps this index a fraction of the table size.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_opportunities_positions "
        "ON opportunities (detected_at DESC) WHERE virtual_shares IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_alerts_sent_at ON alerts (sent_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_intraday_detected_at "
        "ON intraday_opportunities (detected_at DESC)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_intraday_detected_at")
    op.execute("DROP INDEX IF EXISTS ix_alerts_sent_at")
    op.execute("DROP INDEX IF EXISTS ix_opportunities_positions")
    op.execute("DROP INDEX IF EXISTS ix_opportunities_detected_at")
