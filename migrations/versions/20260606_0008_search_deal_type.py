"""Add search deal type.

Revision ID: 20260606_0008
Revises: 20260603_0007
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260606_0008"
down_revision = "20260603_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "searches",
        sa.Column("deal_type", sa.Text(), nullable=False, server_default="rent"),
    )


def downgrade() -> None:
    op.drop_column("searches", "deal_type")
