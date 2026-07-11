"""Backfill Hong Kong coordinates.

Hong Kong was added via the admin UI without nws_lat/nws_lon. Every lat/lon
based collector (GFS, ECMWF, ICON, ensembles, Tomorrow.io, Meteosource) skips
cities with NULL coords, so Hong Kong ran with METAR data only — collector_miss
showed no_data for ALL global models since tracking began.

Coordinates used: Hong Kong Observatory HQ (22.302 N, 114.174 E) — the station
Polymarket's Hong Kong temperature markets resolve against.

Only fills NULLs — never overwrites values set manually via the admin UI.

Revision ID: 018_hong_kong_coords
Revises: 017_virtual_exit
"""
import sqlalchemy as sa
from alembic import op

revision = "018_hong_kong_coords"
down_revision = "017_virtual_exit"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            "UPDATE cities SET nws_lat = 22.302, nws_lon = 114.174 "
            "WHERE name = 'Hong Kong' AND (nws_lat IS NULL OR nws_lon IS NULL)"
        )
    )


def downgrade():
    # Data-only backfill; nothing sensible to revert (we cannot know whether
    # the NULLs were intentional). No-op.
    pass
