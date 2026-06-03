"""Add check run group metrics.

Revision ID: 20260603_0006
Revises: 20260602_0005
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260603_0006"
down_revision = "20260602_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "check_runs",
        sa.Column("active_searches", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "check_runs",
        sa.Column("unique_search_groups", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "check_runs",
        sa.Column("cian_fetches", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "check_runs",
        sa.Column("shared_group_hits", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("check_runs", "shared_group_hits")
    op.drop_column("check_runs", "cian_fetches")
    op.drop_column("check_runs", "unique_search_groups")
    op.drop_column("check_runs", "active_searches")
