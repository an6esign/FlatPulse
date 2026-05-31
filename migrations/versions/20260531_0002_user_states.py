"""Add user state storage.

Revision ID: 20260531_0002
Revises: 20260530_0001
Create Date: 2026-05-31 10:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260531_0002"
down_revision = "20260530_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_states",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("state_key", sa.Text(), primary_key=True),
        sa.Column("state_value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    op.create_index("idx_user_states_user_id", "user_states", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_user_states_user_id", table_name="user_states")
    op.drop_table("user_states")
