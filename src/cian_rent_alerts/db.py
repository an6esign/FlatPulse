from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    case,
    func,
    create_engine,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import SQLAlchemyError

from .models import Listing


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


metadata = MetaData()

listings_table = Table(
    "listings",
    metadata,
    Column("cian_id", Text, primary_key=True),
    Column("url", Text, nullable=False),
    Column("title", Text),
    Column("price", Integer),
    Column("address", Text),
    Column("rooms", Text),
    Column("raw_json", Text, nullable=False, default="{}"),
    Column("first_seen_at", String(40), nullable=False),
    Column("last_seen_at", String(40), nullable=False),
    Column("sent_at", String(40)),
)
Index("idx_listings_sent_at", listings_table.c.sent_at)

app_settings_table = Table(
    "app_settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

check_runs_table = Table(
    "check_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", String(40), nullable=False),
    Column("finished_at", String(40)),
    Column("status", Text, nullable=False),
    Column("listings_found", Integer, nullable=False, default=0),
    Column("listings_saved", Integer, nullable=False, default=0),
    Column("new_listings", Integer, nullable=False, default=0),
    Column("notifications_sent", Integer, nullable=False, default=0),
    Column("active_searches", Integer, nullable=False, default=0),
    Column("unique_search_groups", Integer, nullable=False, default=0),
    Column("cian_fetches", Integer, nullable=False, default=0),
    Column("shared_group_hits", Integer, nullable=False, default=0),
    Column("error", Text),
)
Index("idx_check_runs_started_at", check_runs_table.c.started_at)

users_table = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("telegram_chat_id", Text, nullable=False, unique=True),
    Column("telegram_user_id", Text),
    Column("username", Text),
    Column("first_seen_at", String(40), nullable=False),
    Column("last_seen_at", String(40), nullable=False),
    Column("is_admin", Boolean, nullable=False, default=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("trial_started_at", String(40)),
    Column("trial_ends_at", String(40)),
    Column("paid_until", String(40)),
    Column("subscription_status", Text, nullable=False, default="none"),
)
Index("idx_users_telegram_chat_id", users_table.c.telegram_chat_id)
Index("idx_users_trial_ends_at", users_table.c.trial_ends_at)
Index("idx_users_paid_until", users_table.c.paid_until)

user_states_table = Table(
    "user_states",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("state_key", Text, primary_key=True),
    Column("state_value", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)
Index("idx_user_states_user_id", user_states_table.c.user_id)

searches_table = Table(
    "searches",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("title", Text, nullable=False),
    Column("city", Text, nullable=False),
    Column("region_id", Text),
    Column("rooms", Text, nullable=False),
    Column("min_price", Integer),
    Column("max_price", Integer),
    Column("rent_type", Text, nullable=False),
    Column("sort_by", Text, nullable=False),
    Column("polygon", Text),
    Column("area_label", Text),
    Column("manual_url", Text),
    Column("use_generated_url", Boolean, nullable=False, default=True),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("initialized_at", String(40)),
    Column("last_error_type", Text),
    Column("last_error_at", String(40)),
    Column("cooldown_until", String(40)),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
Index("idx_searches_user_active", searches_table.c.user_id, searches_table.c.is_active)
Index("idx_searches_cooldown_until", searches_table.c.cooldown_until)

search_seen_listings_table = Table(
    "search_seen_listings",
    metadata,
    Column("search_id", Integer, ForeignKey("searches.id"), primary_key=True),
    Column("cian_id", Text, ForeignKey("listings.cian_id"), primary_key=True),
    Column("first_seen_at", String(40), nullable=False),
    Column("sent_at", String(40)),
)
Index("idx_search_seen_sent_at", search_seen_listings_table.c.sent_at)

payments_table = Table(
    "payments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_payment_id", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Column("amount_rub", Integer, nullable=False),
    Column("confirmation_url", Text),
    Column("paid_until", String(40)),
    Column("raw_json", Text, nullable=False, default="{}"),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
Index("idx_payments_user_id", payments_table.c.user_id)
Index("idx_payments_provider_payment_id", payments_table.c.provider_payment_id)

analytics_events_table = Table(
    "analytics_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_name", Text, nullable=False),
    Column("user_id", Integer, ForeignKey("users.id")),
    Column("search_id", Integer, ForeignKey("searches.id")),
    Column("metadata_json", Text, nullable=False, default="{}"),
    Column("created_at", String(40), nullable=False),
)
Index(
    "idx_analytics_events_event_created",
    analytics_events_table.c.event_name,
    analytics_events_table.c.created_at,
)
Index("idx_analytics_events_user_id", analytics_events_table.c.user_id)
Index("idx_analytics_events_search_id", analytics_events_table.c.search_id)


class ListingStore:
    def __init__(self, path: Path, database_url: str | None = None) -> None:
        self.path = path
        self.database_url = build_database_url(path, database_url)
        self.engine = create_engine(self.database_url, future=True, pool_pre_ping=True)

    def init(self) -> None:
        metadata.create_all(self.engine)

    def upsert_many(self, listings: Iterable[Listing]) -> int:
        now = utc_now()
        rows = list(listings)
        with self.engine.begin() as conn:
            for listing in rows:
                payload = {
                    "cian_id": listing.cian_id,
                    "url": listing.url,
                    "title": listing.title,
                    "price": listing.price,
                    "address": listing.address,
                    "rooms": listing.rooms,
                    "raw_json": json.dumps(listing.raw, ensure_ascii=False),
                    "last_seen_at": now,
                }
                exists = conn.execute(
                    select(listings_table.c.cian_id).where(
                        listings_table.c.cian_id == listing.cian_id
                    )
                ).first()
                if exists:
                    conn.execute(
                        update(listings_table)
                        .where(listings_table.c.cian_id == listing.cian_id)
                        .values(**payload)
                    )
                else:
                    conn.execute(
                        insert(listings_table).values(
                            **payload,
                            first_seen_at=now,
                        )
                    )
        return len(rows)

    def unsent(self, limit: int) -> list[Listing]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    listings_table.c.cian_id,
                    listings_table.c.url,
                    listings_table.c.title,
                    listings_table.c.price,
                    listings_table.c.address,
                    listings_table.c.rooms,
                    listings_table.c.raw_json,
                )
                .where(listings_table.c.sent_at.is_(None))
                .order_by(listings_table.c.first_seen_at.asc())
                .limit(limit)
            ).fetchall()

        return [self._row_to_listing(dict(row._mapping)) for row in rows]

    def mark_sent(self, cian_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(listings_table)
                .where(listings_table.c.cian_id == cian_id)
                .values(sent_at=utc_now())
            )

    def mark_all_unsent_as_sent(self) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(listings_table)
                .where(listings_table.c.sent_at.is_(None))
                .values(sent_at=utc_now())
            )
            return result.rowcount or 0

    def get_runtime_settings(self) -> dict[str, str]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(app_settings_table.c.key, app_settings_table.c.value)
            ).fetchall()
        return {row._mapping["key"]: row._mapping["value"] for row in rows}

    def set_runtime_setting(self, key: str, value: str) -> None:
        now = utc_now()
        with self.engine.begin() as conn:
            exists = conn.execute(
                select(app_settings_table.c.key).where(app_settings_table.c.key == key)
            ).first()
            if exists:
                conn.execute(
                    update(app_settings_table)
                    .where(app_settings_table.c.key == key)
                    .values(value=value, updated_at=now)
                )
            else:
                conn.execute(
                    insert(app_settings_table).values(key=key, value=value, updated_at=now)
                )

    def delete_runtime_setting(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(app_settings_table).where(app_settings_table.c.key == key))

    def clear_runtime_settings(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(app_settings_table))

    def ping(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(select(1))

    def schema_version(self) -> str | None:
        try:
            with self.engine.begin() as conn:
                value = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except SQLAlchemyError:
            return None
        return str(value) if value is not None else None

    def start_check_run(self) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(check_runs_table).values(started_at=utc_now(), status="running")
            )
            return int(result.inserted_primary_key[0])

    def finish_check_run(
        self,
        run_id: int,
        *,
        status: str,
        listings_found: int = 0,
        listings_saved: int = 0,
        new_listings: int = 0,
        notifications_sent: int = 0,
        active_searches: int = 0,
        unique_search_groups: int = 0,
        cian_fetches: int = 0,
        shared_group_hits: int = 0,
        error: str | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(check_runs_table)
                .where(check_runs_table.c.id == run_id)
                .values(
                    finished_at=utc_now(),
                    status=status,
                    listings_found=listings_found,
                    listings_saved=listings_saved,
                    new_listings=new_listings,
                    notifications_sent=notifications_sent,
                    active_searches=active_searches,
                    unique_search_groups=unique_search_groups,
                    cian_fetches=cian_fetches,
                    shared_group_hits=shared_group_hits,
                    error=error,
                )
            )

    def last_check_run(self) -> dict[str, object] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(check_runs_table).order_by(check_runs_table.c.started_at.desc()).limit(1)
            ).first()
        return dict(row._mapping) if row is not None else None

    def get_check_run(self, run_id: int) -> dict[str, object] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(check_runs_table).where(check_runs_table.c.id == run_id)
            ).first()
        return dict(row._mapping) if row is not None else None

    def recent_check_runs(self, limit: int = 5) -> list[dict[str, object]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(check_runs_table).order_by(check_runs_table.c.started_at.desc()).limit(limit)
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def recent_failed_check_runs(self, limit: int = 5) -> list[dict[str, object]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(check_runs_table)
                .where(check_runs_table.c.status == "failed")
                .order_by(check_runs_table.c.started_at.desc())
                .limit(limit)
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def check_runs_summary_since(self, since: str) -> dict[str, int]:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(
                    func.count().label("runs"),
                    func.coalesce(func.sum(check_runs_table.c.listings_found), 0).label(
                        "listings_found"
                    ),
                    func.coalesce(func.sum(check_runs_table.c.new_listings), 0).label(
                        "new_listings"
                    ),
                    func.coalesce(func.sum(check_runs_table.c.notifications_sent), 0).label(
                        "notifications_sent"
                    ),
                    func.coalesce(func.sum(check_runs_table.c.active_searches), 0).label(
                        "active_searches"
                    ),
                    func.coalesce(func.sum(check_runs_table.c.unique_search_groups), 0).label(
                        "unique_search_groups"
                    ),
                    func.coalesce(func.sum(check_runs_table.c.cian_fetches), 0).label(
                        "cian_fetches"
                    ),
                    func.coalesce(func.sum(check_runs_table.c.shared_group_hits), 0).label(
                        "shared_group_hits"
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (check_runs_table.c.status == "failed", 1),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("failed_runs"),
                    func.coalesce(
                        func.sum(
                            case(
                                (check_runs_table.c.status == "partial", 1),
                                else_=0,
                            )
                        ),
                        0,
                    ).label("partial_runs"),
                ).where(check_runs_table.c.started_at >= since)
            ).first()
        if row is None:
            return {
                "runs": 0,
                "listings_found": 0,
                "new_listings": 0,
                "notifications_sent": 0,
                "active_searches": 0,
                "unique_search_groups": 0,
                "cian_fetches": 0,
                "shared_group_hits": 0,
                "failed_runs": 0,
                "partial_runs": 0,
            }
        data = row._mapping
        return {
            "runs": int(data["runs"] or 0),
            "listings_found": int(data["listings_found"] or 0),
            "new_listings": int(data["new_listings"] or 0),
            "notifications_sent": int(data["notifications_sent"] or 0),
            "active_searches": int(data["active_searches"] or 0),
            "unique_search_groups": int(data["unique_search_groups"] or 0),
            "cian_fetches": int(data["cian_fetches"] or 0),
            "shared_group_hits": int(data["shared_group_hits"] or 0),
            "failed_runs": int(data["failed_runs"] or 0),
            "partial_runs": int(data["partial_runs"] or 0),
        }

    def record_event(
        self,
        event_name: str,
        *,
        user_id: int | None = None,
        search_id: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> int:
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(analytics_events_table).values(
                    event_name=event_name,
                    user_id=user_id,
                    search_id=search_id,
                    metadata_json=payload,
                    created_at=utc_now(),
                )
            )
            return int(result.inserted_primary_key[0])

    def analytics_events_summary_since(self, since: str) -> dict[str, int]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    analytics_events_table.c.event_name,
                    func.count().label("count"),
                )
                .where(analytics_events_table.c.created_at >= since)
                .group_by(analytics_events_table.c.event_name)
            ).fetchall()
        return {str(row._mapping["event_name"]): int(row._mapping["count"] or 0) for row in rows}

    def upsert_user(
        self,
        *,
        telegram_chat_id: str,
        telegram_user_id: str | None = None,
        username: str | None = None,
        is_admin: bool = False,
    ) -> int:
        now = utc_now()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(users_table.c.id).where(users_table.c.telegram_chat_id == telegram_chat_id)
            ).first()
            if row is not None:
                user_id = int(row._mapping["id"])
                conn.execute(
                    update(users_table)
                    .where(users_table.c.id == user_id)
                    .values(
                        telegram_user_id=telegram_user_id,
                        username=username,
                        last_seen_at=now,
                        is_admin=is_admin,
                        is_active=True,
                    )
                )
                return user_id

            result = conn.execute(
                insert(users_table).values(
                    telegram_chat_id=telegram_chat_id,
                    telegram_user_id=telegram_user_id,
                    username=username,
                    first_seen_at=now,
                    last_seen_at=now,
                    is_admin=is_admin,
                    is_active=True,
                    subscription_status="none",
                )
            )
            return int(result.inserted_primary_key[0])

    def get_user_by_chat_id(self, telegram_chat_id: str) -> dict[str, object] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(users_table).where(users_table.c.telegram_chat_id == telegram_chat_id)
            ).first()
        return dict(row._mapping) if row is not None else None

    def get_user(self, user_id: int) -> dict[str, object] | None:
        with self.engine.begin() as conn:
            row = conn.execute(select(users_table).where(users_table.c.id == user_id)).first()
        return dict(row._mapping) if row is not None else None

    def start_trial_if_needed(self, user_id: int, *, days: int) -> dict[str, object] | None:
        user = self.get_user(user_id)
        if user is None:
            return None
        if user.get("trial_started_at") or self.user_has_active_paid_access(user_id):
            return user

        now = datetime.now(UTC)
        trial_ends_at = now + timedelta(days=days)
        with self.engine.begin() as conn:
            conn.execute(
                update(users_table)
                .where(users_table.c.id == user_id)
                .values(
                    trial_started_at=now.isoformat(timespec="seconds"),
                    trial_ends_at=trial_ends_at.isoformat(timespec="seconds"),
                    subscription_status="trial",
                    last_seen_at=now.isoformat(timespec="seconds"),
                )
            )
        return self.get_user(user_id)

    def user_has_active_access(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if user is None:
            return False
        if bool(user.get("is_admin")):
            return True
        now = datetime.now(UTC)
        trial_ends_at = _parse_datetime(user.get("trial_ends_at"))
        if trial_ends_at is not None and trial_ends_at > now:
            return True
        paid_until = _parse_datetime(user.get("paid_until"))
        return paid_until is not None and paid_until > now

    def user_has_used_trial(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return bool(user and user.get("trial_started_at"))

    def user_has_active_trial(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if user is None:
            return False
        trial_ends_at = _parse_datetime(user.get("trial_ends_at"))
        return trial_ends_at is not None and trial_ends_at > datetime.now(UTC)

    def user_has_active_paid_access(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if user is None:
            return False
        paid_until = _parse_datetime(user.get("paid_until"))
        return paid_until is not None and paid_until > datetime.now(UTC)

    def grant_paid_access(self, user_id: int, *, days: int) -> str | None:
        user = self.get_user(user_id)
        if user is None:
            return None
        now = datetime.now(UTC)
        current_paid_until = _parse_datetime(user.get("paid_until"))
        starts_at = current_paid_until if current_paid_until and current_paid_until > now else now
        paid_until = starts_at + timedelta(days=days)
        value = paid_until.isoformat(timespec="seconds")
        with self.engine.begin() as conn:
            conn.execute(
                update(users_table)
                .where(users_table.c.id == user_id)
                .values(
                    paid_until=value,
                    subscription_status="paid",
                    last_seen_at=now.isoformat(timespec="seconds"),
                )
            )
        return value

    def create_payment(
        self,
        *,
        user_id: int,
        provider_payment_id: str,
        status: str,
        amount_rub: int,
        confirmation_url: str | None,
        raw_json: str,
    ) -> int:
        now = utc_now()
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(payments_table).values(
                    user_id=user_id,
                    provider="yookassa",
                    provider_payment_id=provider_payment_id,
                    status=status,
                    amount_rub=amount_rub,
                    confirmation_url=confirmation_url,
                    raw_json=raw_json,
                    created_at=now,
                    updated_at=now,
                )
            )
            return int(result.inserted_primary_key[0])

    def update_payment(
        self,
        provider_payment_id: str,
        *,
        status: str,
        paid_until: str | None = None,
        raw_json: str | None = None,
    ) -> None:
        values: dict[str, object] = {"status": status, "updated_at": utc_now()}
        if paid_until is not None:
            values["paid_until"] = paid_until
        if raw_json is not None:
            values["raw_json"] = raw_json
        with self.engine.begin() as conn:
            conn.execute(
                update(payments_table)
                .where(payments_table.c.provider_payment_id == provider_payment_id)
                .values(**values)
            )

    def latest_pending_payment_for_user(self, user_id: int) -> dict[str, object] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(payments_table)
                .where(payments_table.c.user_id == user_id)
                .where(payments_table.c.status.in_(["pending", "waiting_for_capture"]))
                .order_by(payments_table.c.created_at.desc(), payments_table.c.id.desc())
                .limit(1)
            ).first()
        return dict(row._mapping) if row is not None else None

    def users_count(self, *, active: bool | None = None) -> int:
        query = select(func.count()).select_from(users_table)
        if active is not None:
            query = query.where(users_table.c.is_active.is_(active))
        with self.engine.begin() as conn:
            return int(conn.execute(query).scalar_one())

    def recent_users(self, limit: int = 10) -> list[dict[str, object]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(users_table).order_by(users_table.c.last_seen_at.desc()).limit(limit)
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def get_user_state(self, user_id: int, key: str) -> str | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(user_states_table.c.state_value)
                .where(user_states_table.c.user_id == user_id)
                .where(user_states_table.c.state_key == key)
            ).first()
        if row is None:
            return None
        return str(row._mapping["state_value"])

    def set_user_state(self, user_id: int, key: str, value: str) -> None:
        now = utc_now()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(user_states_table.c.user_id)
                .where(user_states_table.c.user_id == user_id)
                .where(user_states_table.c.state_key == key)
            ).first()
            if row is not None:
                conn.execute(
                    update(user_states_table)
                    .where(user_states_table.c.user_id == user_id)
                    .where(user_states_table.c.state_key == key)
                    .values(state_value=value, updated_at=now)
                )
                return

            conn.execute(
                insert(user_states_table).values(
                    user_id=user_id,
                    state_key=key,
                    state_value=value,
                    updated_at=now,
                )
            )

    def delete_user_state(self, user_id: int, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                delete(user_states_table)
                .where(user_states_table.c.user_id == user_id)
                .where(user_states_table.c.state_key == key)
            )

    def create_search(
        self,
        *,
        user_id: int,
        title: str,
        city: str,
        region_id: str | None,
        rooms: tuple[str, ...],
        min_price: int | None,
        max_price: int | None,
        rent_type: str,
        sort_by: str,
        polygon: str | None = None,
        area_label: str | None = None,
        manual_url: str | None = None,
        use_generated_url: bool = True,
        is_active: bool = True,
    ) -> int:
        now = utc_now()
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(searches_table).values(
                    user_id=user_id,
                    title=title,
                    city=city,
                    region_id=region_id,
                    rooms=",".join(rooms),
                    min_price=min_price,
                    max_price=max_price,
                    rent_type=rent_type,
                    sort_by=sort_by,
                    polygon=polygon,
                    area_label=area_label,
                    manual_url=manual_url,
                    use_generated_url=use_generated_url,
                    is_active=is_active,
                    created_at=now,
                    updated_at=now,
                )
            )
            return int(result.inserted_primary_key[0])

    def update_search(self, search_id: int, **values: object) -> None:
        allowed_keys = {
            "title",
            "city",
            "region_id",
            "rooms",
            "min_price",
            "max_price",
            "rent_type",
            "sort_by",
            "polygon",
            "area_label",
            "manual_url",
            "use_generated_url",
            "is_active",
            "initialized_at",
            "last_error_type",
            "last_error_at",
            "cooldown_until",
        }
        unknown_keys = set(values) - allowed_keys
        if unknown_keys:
            unknown = ", ".join(sorted(unknown_keys))
            raise ValueError(f"Unknown search fields: {unknown}")

        payload = dict(values)
        if isinstance(payload.get("rooms"), tuple):
            payload["rooms"] = ",".join(str(room) for room in payload["rooms"])
        payload["updated_at"] = utc_now()

        with self.engine.begin() as conn:
            conn.execute(
                update(searches_table).where(searches_table.c.id == search_id).values(**payload)
            )

    def record_search_success(self, search_id: int, *, initialize: bool = False) -> None:
        values: dict[str, object] = {
            "last_error_type": None,
            "last_error_at": None,
            "cooldown_until": None,
        }
        if initialize:
            values["initialized_at"] = utc_now()
        self.update_search(search_id, **values)

    def record_search_error(
        self,
        search_id: int,
        *,
        error_type: str,
        cooldown_until: str | None,
    ) -> None:
        self.update_search(
            search_id,
            last_error_type=error_type,
            last_error_at=utc_now(),
            cooldown_until=cooldown_until,
        )

    def active_searches_for_user(self, user_id: int) -> list[dict[str, object]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(searches_table)
                .where(searches_table.c.user_id == user_id)
                .where(searches_table.c.is_active.is_(True))
                .order_by(searches_table.c.created_at.asc())
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def searches_for_user(self, user_id: int) -> list[dict[str, object]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(searches_table)
                .where(searches_table.c.user_id == user_id)
                .order_by(searches_table.c.created_at.asc())
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def first_search_for_user(self, user_id: int) -> dict[str, object] | None:
        searches = self.searches_for_user(user_id)
        return searches[0] if searches else None

    def current_search_for_user(self, user_id: int) -> dict[str, object] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(searches_table)
                .where(searches_table.c.user_id == user_id)
                .order_by(searches_table.c.updated_at.desc(), searches_table.c.id.desc())
                .limit(1)
            ).first()
        return dict(row._mapping) if row is not None else None

    def first_active_search_for_user(self, user_id: int) -> dict[str, object] | None:
        searches = self.active_searches_for_user(user_id)
        return searches[0] if searches else None

    def deactivate_other_searches_for_user(self, user_id: int, keep_search_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(searches_table)
                .where(searches_table.c.user_id == user_id)
                .where(searches_table.c.id != keep_search_id)
                .where(searches_table.c.is_active.is_(True))
                .values(is_active=False, updated_at=utc_now())
            )

    def searches_count(self) -> int:
        return self.searches_count_by_status()

    def searches_count_by_status(self, *, active: bool | None = None) -> int:
        query = select(func.count()).select_from(searches_table)
        if active is not None:
            query = query.where(searches_table.c.is_active.is_(active))
        with self.engine.begin() as conn:
            return int(conn.execute(query).scalar_one())

    def recent_searches(self, limit: int = 10) -> list[dict[str, object]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    searches_table.c.id.label("id"),
                    searches_table.c.city.label("city"),
                    searches_table.c.rooms.label("rooms"),
                    searches_table.c.min_price.label("min_price"),
                    searches_table.c.max_price.label("max_price"),
                    searches_table.c.rent_type.label("rent_type"),
                    searches_table.c.is_active.label("is_active"),
                    searches_table.c.last_error_type.label("last_error_type"),
                    searches_table.c.last_error_at.label("last_error_at"),
                    searches_table.c.cooldown_until.label("cooldown_until"),
                    searches_table.c.updated_at.label("updated_at"),
                    users_table.c.telegram_chat_id.label("telegram_chat_id"),
                    users_table.c.username.label("username"),
                )
                .select_from(searches_table.join(users_table))
                .order_by(searches_table.c.updated_at.desc())
                .limit(limit)
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def active_searches(self) -> list[dict[str, object]]:
        current_searches = (
            select(
                searches_table.c.user_id.label("user_id"),
                func.max(searches_table.c.id).label("search_id"),
            )
            .where(searches_table.c.is_active.is_(True))
            .group_by(searches_table.c.user_id)
            .subquery()
        )
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    searches_table.c.id.label("id"),
                    searches_table.c.user_id.label("user_id"),
                    searches_table.c.title.label("title"),
                    searches_table.c.city.label("city"),
                    searches_table.c.region_id.label("region_id"),
                    searches_table.c.rooms.label("rooms"),
                    searches_table.c.min_price.label("min_price"),
                    searches_table.c.max_price.label("max_price"),
                    searches_table.c.rent_type.label("rent_type"),
                    searches_table.c.sort_by.label("sort_by"),
                    searches_table.c.polygon.label("polygon"),
                    searches_table.c.area_label.label("area_label"),
                    searches_table.c.manual_url.label("manual_url"),
                    searches_table.c.use_generated_url.label("use_generated_url"),
                    searches_table.c.is_active.label("is_active"),
                    searches_table.c.initialized_at.label("initialized_at"),
                    searches_table.c.last_error_type.label("last_error_type"),
                    searches_table.c.last_error_at.label("last_error_at"),
                    searches_table.c.cooldown_until.label("cooldown_until"),
                    users_table.c.telegram_chat_id.label("telegram_chat_id"),
                )
                .select_from(
                    searches_table.join(users_table).join(
                        current_searches,
                        (current_searches.c.user_id == searches_table.c.user_id)
                        & (current_searches.c.search_id == searches_table.c.id),
                    )
                )
                .where(searches_table.c.is_active.is_(True))
                .where(users_table.c.is_active.is_(True))
                .where(
                    (searches_table.c.cooldown_until.is_(None))
                    | (searches_table.c.cooldown_until <= utc_now())
                )
                .order_by(searches_table.c.id.asc())
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def cooldown_searches_count(self) -> int:
        now = utc_now()
        with self.engine.begin() as conn:
            return int(
                conn.execute(
                    select(func.count())
                    .select_from(searches_table)
                    .where(searches_table.c.is_active.is_(True))
                    .where(searches_table.c.cooldown_until.is_not(None))
                    .where(searches_table.c.cooldown_until > now)
                ).scalar_one()
            )

    def search_seen_count(self, search_id: int) -> int:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(search_seen_listings_table.c.cian_id).where(
                    search_seen_listings_table.c.search_id == search_id
                )
            ).fetchall()
        return len(rows)

    def unseen_listings_for_search(
        self, search_id: int, listings: Iterable[Listing]
    ) -> list[Listing]:
        rows = list(listings)
        if not rows:
            return []

        listing_ids = [listing.cian_id for listing in rows]
        with self.engine.begin() as conn:
            seen_rows = conn.execute(
                select(search_seen_listings_table.c.cian_id)
                .where(search_seen_listings_table.c.search_id == search_id)
                .where(search_seen_listings_table.c.cian_id.in_(listing_ids))
            ).fetchall()

        seen_ids = {str(row._mapping["cian_id"]) for row in seen_rows}
        return [listing for listing in rows if listing.cian_id not in seen_ids]

    def listings_seen_for_search(self, search_id: int, limit: int) -> list[Listing]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    listings_table.c.cian_id,
                    listings_table.c.url,
                    listings_table.c.title,
                    listings_table.c.price,
                    listings_table.c.address,
                    listings_table.c.rooms,
                    listings_table.c.raw_json,
                )
                .select_from(
                    search_seen_listings_table.join(
                        listings_table,
                        search_seen_listings_table.c.cian_id == listings_table.c.cian_id,
                    )
                )
                .where(search_seen_listings_table.c.search_id == search_id)
                .order_by(search_seen_listings_table.c.first_seen_at.desc())
                .limit(limit)
            ).fetchall()

        return [self._row_to_listing(dict(row._mapping)) for row in rows]

    def mark_many_search_listings_seen(
        self,
        *,
        search_id: int,
        cian_ids: Iterable[str],
        sent: bool = False,
    ) -> int:
        count = 0
        for cian_id in cian_ids:
            self.mark_search_listing_seen(search_id=search_id, cian_id=cian_id, sent=sent)
            count += 1
        return count

    def mark_all_listings_seen_for_search(self, search_id: int, *, sent: bool = False) -> int:
        with self.engine.begin() as conn:
            rows = conn.execute(select(listings_table.c.cian_id)).fetchall()
        return self.mark_many_search_listings_seen(
            search_id=search_id,
            cian_ids=[str(row._mapping["cian_id"]) for row in rows],
            sent=sent,
        )

    def clear_seen_for_search(self, search_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                delete(search_seen_listings_table).where(
                    search_seen_listings_table.c.search_id == search_id
                )
            )

    def mark_search_listing_seen(
        self,
        *,
        search_id: int,
        cian_id: str,
        sent: bool = False,
    ) -> None:
        now = utc_now()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(search_seen_listings_table.c.search_id)
                .where(search_seen_listings_table.c.search_id == search_id)
                .where(search_seen_listings_table.c.cian_id == cian_id)
            ).first()
            if row is not None:
                if sent:
                    conn.execute(
                        update(search_seen_listings_table)
                        .where(search_seen_listings_table.c.search_id == search_id)
                        .where(search_seen_listings_table.c.cian_id == cian_id)
                        .values(sent_at=now)
                    )
                return

            conn.execute(
                insert(search_seen_listings_table).values(
                    search_id=search_id,
                    cian_id=cian_id,
                    first_seen_at=now,
                    sent_at=now if sent else None,
                )
            )

    @staticmethod
    def _row_to_listing(row: dict[str, object]) -> Listing:
        try:
            raw = json.loads(str(row["raw_json"]))
        except json.JSONDecodeError:
            raw = {}
        return Listing(
            cian_id=str(row["cian_id"]),
            url=str(row["url"]),
            title=row["title"] if isinstance(row["title"], str) else None,
            price=row["price"] if isinstance(row["price"], int) else None,
            address=row["address"] if isinstance(row["address"], str) else None,
            rooms=row["rooms"] if isinstance(row["rooms"], str) else None,
            raw=raw,
        )


def _sqlite_url(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.absolute()}"


def build_database_url(path: Path, database_url: str | None = None) -> str:
    if database_url:
        return _normalize_database_url(database_url)
    return _sqlite_url(path)


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
