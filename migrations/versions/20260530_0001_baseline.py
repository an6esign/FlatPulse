"""Create baseline schema.

Revision ID: 20260530_0001
Revises:
Create Date: 2026-05-30 17:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260530_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    op.create_table(
        "check_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("finished_at", sa.String(length=40)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("listings_found", sa.Integer(), nullable=False),
        sa.Column("listings_saved", sa.Integer(), nullable=False),
        sa.Column("new_listings", sa.Integer(), nullable=False),
        sa.Column("notifications_sent", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("idx_check_runs_started_at", "check_runs", ["started_at"])
    op.create_table(
        "listings",
        sa.Column("cian_id", sa.Text(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("price", sa.Integer()),
        sa.Column("address", sa.Text()),
        sa.Column("rooms", sa.Text()),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.String(length=40), nullable=False),
        sa.Column("last_seen_at", sa.String(length=40), nullable=False),
        sa.Column("sent_at", sa.String(length=40)),
    )
    op.create_index("idx_listings_sent_at", "listings", ["sent_at"])
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_chat_id", sa.Text(), nullable=False, unique=True),
        sa.Column("telegram_user_id", sa.Text()),
        sa.Column("username", sa.Text()),
        sa.Column("first_seen_at", sa.String(length=40), nullable=False),
        sa.Column("last_seen_at", sa.String(length=40), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )
    op.create_index("idx_users_telegram_chat_id", "users", ["telegram_chat_id"])
    op.create_table(
        "searches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=False),
        sa.Column("region_id", sa.Text()),
        sa.Column("rooms", sa.Text(), nullable=False),
        sa.Column("min_price", sa.Integer()),
        sa.Column("max_price", sa.Integer()),
        sa.Column("rent_type", sa.Text(), nullable=False),
        sa.Column("sort_by", sa.Text(), nullable=False),
        sa.Column("polygon", sa.Text()),
        sa.Column("area_label", sa.Text()),
        sa.Column("manual_url", sa.Text()),
        sa.Column("use_generated_url", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    op.create_index("idx_searches_user_active", "searches", ["user_id", "is_active"])
    op.create_table(
        "search_seen_listings",
        sa.Column("search_id", sa.Integer(), sa.ForeignKey("searches.id"), primary_key=True),
        sa.Column("cian_id", sa.Text(), sa.ForeignKey("listings.cian_id"), primary_key=True),
        sa.Column("first_seen_at", sa.String(length=40), nullable=False),
        sa.Column("sent_at", sa.String(length=40)),
    )
    op.create_index("idx_search_seen_sent_at", "search_seen_listings", ["sent_at"])


def downgrade() -> None:
    op.drop_index("idx_search_seen_sent_at", table_name="search_seen_listings")
    op.drop_table("search_seen_listings")
    op.drop_index("idx_searches_user_active", table_name="searches")
    op.drop_table("searches")
    op.drop_index("idx_users_telegram_chat_id", table_name="users")
    op.drop_table("users")
    op.drop_index("idx_listings_sent_at", table_name="listings")
    op.drop_table("listings")
    op.drop_index("idx_check_runs_started_at", table_name="check_runs")
    op.drop_table("check_runs")
    op.drop_table("app_settings")
