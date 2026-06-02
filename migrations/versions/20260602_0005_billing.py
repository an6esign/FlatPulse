"""Add billing and subscription state.

Revision ID: 20260602_0005
Revises: 20260531_0004
Create Date: 2026-06-02 00:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260602_0005"
down_revision = "20260531_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("trial_started_at", sa.String(length=40)))
    op.add_column("users", sa.Column("trial_ends_at", sa.String(length=40)))
    op.add_column("users", sa.Column("paid_until", sa.String(length=40)))
    op.add_column(
        "users",
        sa.Column(
            "subscription_status",
            sa.Text(),
            nullable=False,
            server_default="none",
        ),
    )
    op.create_index("idx_users_trial_ends_at", "users", ["trial_ends_at"])
    op.create_index("idx_users_paid_until", "users", ["paid_until"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_payment_id", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("amount_rub", sa.Integer(), nullable=False),
        sa.Column("confirmation_url", sa.Text()),
        sa.Column("paid_until", sa.String(length=40)),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    op.create_index("idx_payments_user_id", "payments", ["user_id"])
    op.create_index("idx_payments_provider_payment_id", "payments", ["provider_payment_id"])


def downgrade() -> None:
    op.drop_index("idx_payments_provider_payment_id", table_name="payments")
    op.drop_index("idx_payments_user_id", table_name="payments")
    op.drop_table("payments")
    op.drop_index("idx_users_paid_until", table_name="users")
    op.drop_index("idx_users_trial_ends_at", table_name="users")
    op.drop_column("users", "subscription_status")
    op.drop_column("users", "paid_until")
    op.drop_column("users", "trial_ends_at")
    op.drop_column("users", "trial_started_at")
