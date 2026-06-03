"""Add analytics events.

Revision ID: 20260603_0007
Revises: 20260603_0006
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260603_0007"
down_revision = "20260603_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("search_id", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["search_id"], ["searches.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_analytics_events_event_created",
        "analytics_events",
        ["event_name", "created_at"],
    )
    op.create_index("idx_analytics_events_user_id", "analytics_events", ["user_id"])
    op.create_index("idx_analytics_events_search_id", "analytics_events", ["search_id"])


def downgrade() -> None:
    op.drop_index("idx_analytics_events_search_id", table_name="analytics_events")
    op.drop_index("idx_analytics_events_user_id", table_name="analytics_events")
    op.drop_index("idx_analytics_events_event_created", table_name="analytics_events")
    op.drop_table("analytics_events")
