"""Add search initialization marker.

Revision ID: 20260531_0004
Revises: 20260531_0003
Create Date: 2026-05-31 17:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260531_0004"
down_revision = "20260531_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("searches", sa.Column("initialized_at", sa.String(length=40)))
    op.execute(
        """
        UPDATE searches
        SET initialized_at = updated_at
        WHERE EXISTS (
            SELECT 1
            FROM search_seen_listings
            WHERE search_seen_listings.search_id = searches.id
        )
        """
    )


def downgrade() -> None:
    op.drop_column("searches", "initialized_at")
