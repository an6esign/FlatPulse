"""Add search cooldown fields.

Revision ID: 20260531_0003
Revises: 20260531_0002
Create Date: 2026-05-31 14:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260531_0003"
down_revision = "20260531_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("searches", sa.Column("last_error_type", sa.Text()))
    op.add_column("searches", sa.Column("last_error_at", sa.String(length=40)))
    op.add_column("searches", sa.Column("cooldown_until", sa.String(length=40)))
    op.create_index("idx_searches_cooldown_until", "searches", ["cooldown_until"])


def downgrade() -> None:
    op.drop_index("idx_searches_cooldown_until", table_name="searches")
    op.drop_column("searches", "cooldown_until")
    op.drop_column("searches", "last_error_at")
    op.drop_column("searches", "last_error_type")
