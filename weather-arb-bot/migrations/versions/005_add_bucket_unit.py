"""Add bucket_unit to market_outcomes and backfill Celsius bounds.

Revision ID: 005
Revises: 004
Create Date: 2026-05-26 00:00:00.000000

Fixes a class of resolution bugs where Celsius bucket bounds were stored
as ROUNDED Fahrenheit values (via _c_to_f). Adjacent Celsius buckets then
could share a Fahrenheit integer at the boundary, causing both buckets to
'win' for actual readings near that boundary (e.g. London 91.4F = 33.0C
incorrectly resolved both 32C and 33C as YES wins).

Fix:
- Add `bucket_unit` column ('F' or 'C', default 'F').
- Backfill: detect Celsius labels from `bucket_label`, set unit='C',
  and replace bucket_min/bucket_max with the ORIGINAL Celsius integers
  parsed from the label.

Resolution and detection code reads bucket_unit and converts as needed.
"""
import re
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_C_PATTERN = re.compile(r"\d\s*c(?:\s|$|or\b|/)", re.IGNORECASE)


def _is_celsius_label(label):
    if not label:
        return False
    lo = label.lower()
    if "°c" in lo or "celsius" in lo:
        return True
    return bool(_C_PATTERN.search(lo))


def _parse_c_bounds(label):
    """Return (bmin, bmax) in native Celsius integers, or (None, None)."""
    if not label:
        return None, None
    t = label.lower().replace("°", "").strip()
    m = re.search(r"(\d+)\s*(?:-|to|–)\s*(\d+)\s*[fc]?", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\s*\+", t)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(\d+)\s*[fc]?\s*(?:or\s+)?(?:above|higher|over|more|greater)", t)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?:above|over|greater\s+than)\s+(\d+)", t)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(\d+)\s*[fc]?\s*(?:or\s+)?(?:below|lower|under|less)", t)
    if m:
        return None, int(m.group(1))
    m = re.search(r"(?:below|under|less\s+than)\s+(\d+)", t)
    if m:
        return None, int(m.group(1))
    nums = re.findall(r"\d+", t)
    if nums:
        v = int(nums[0])
        return v, v
    return None, None


def upgrade() -> None:
    op.add_column(
        "market_outcomes",
        sa.Column("bucket_unit", sa.String(1), nullable=False, server_default="F"),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, bucket_label FROM market_outcomes")
    ).fetchall()
    for row in rows:
        label = row.bucket_label or ""
        if not _is_celsius_label(label):
            continue
        bmin, bmax = _parse_c_bounds(label)
        conn.execute(
            sa.text(
                "UPDATE market_outcomes "
                "SET bucket_unit = 'C', bucket_min = :bmin, bucket_max = :bmax "
                "WHERE id = :id"
            ),
            {"bmin": bmin, "bmax": bmax, "id": row.id},
        )


def downgrade() -> None:
    op.drop_column("market_outcomes", "bucket_unit")
