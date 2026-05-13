"""Add token_id to market_outcomes

Revision ID: 003
Revises: 002
Create Date: 2026-05-13 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("market_outcomes", sa.Column("token_id", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("market_outcomes", "token_id")
