"""Add virtual position fields to opportunities

Revision ID: 004
Revises: 003
Create Date: 2026-05-25 00:00:00.000000

Adds simulated 5-share position tracking to the opportunities table:
each alert above the buy-confidence threshold gets a virtual position
which is settled at resolution time to compute realised P&L.

All columns are nullable so legacy rows (and rows whose confidence falls
between the alert threshold and the buy threshold) keep working.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("opportunities", sa.Column("virtual_shares", sa.Integer(), nullable=True))
    op.add_column("opportunities", sa.Column("virtual_entry_price", sa.Float(), nullable=True))
    op.add_column("opportunities", sa.Column("virtual_cost", sa.Float(), nullable=True))
    op.add_column("opportunities", sa.Column("virtual_payout", sa.Float(), nullable=True))
    op.add_column("opportunities", sa.Column("virtual_pnl", sa.Float(), nullable=True))
    op.add_column("opportunities", sa.Column("virtual_status", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("opportunities", "virtual_status")
    op.drop_column("opportunities", "virtual_pnl")
    op.drop_column("opportunities", "virtual_payout")
    op.drop_column("opportunities", "virtual_cost")
    op.drop_column("opportunities", "virtual_entry_price")
    op.drop_column("opportunities", "virtual_shares")
