from pathlib import Path
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from cian_rent_alerts.db import ListingStore, users_table
from cian_rent_alerts.models import Listing


def test_user_search_and_seen_listing_flow(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()

    user_id = store.upsert_user(
        telegram_chat_id="100",
        telegram_user_id="200",
        username="tester",
        is_admin=True,
    )
    same_user_id = store.upsert_user(
        telegram_chat_id="100",
        telegram_user_id="200",
        username="tester2",
        is_admin=True,
    )

    assert same_user_id == user_id

    search_id = store.create_search(
        user_id=user_id,
        title="Казань рядом с работой",
        city="Казань",
        region_id="4777",
        rooms=("1", "2"),
        min_price=35000,
        max_price=45000,
        rent_type="long",
        sort_by="creation_date_from_newer_to_older",
        polygon="49.1_55.7,49.2_55.7,49.2_55.8",
        area_label="Калинина 23, 1000 м",
    )
    inactive_search_id = store.create_search(
        user_id=user_id,
        title="Черновик",
        city="Казань",
        region_id="4777",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
        is_active=False,
    )

    searches = store.active_searches_for_user(user_id)
    all_searches = store.searches_for_user(user_id)

    assert len(searches) == 1
    assert searches[0]["id"] == search_id
    assert searches[0]["title"] == "Казань рядом с работой"
    assert searches[0]["rooms"] == "1,2"
    assert {search["id"] for search in all_searches} == {search_id, inactive_search_id}

    store.upsert_many(
        [
            Listing(
                cian_id="123",
                url="https://www.cian.ru/rent/flat/123/",
                title="1-комн. квартира",
            )
        ]
    )
    store.mark_search_listing_seen(search_id=search_id, cian_id="123", sent=True)
    store.mark_search_listing_seen(search_id=search_id, cian_id="123", sent=True)

    assert store.search_seen_count(search_id) == 1
    assert [listing.cian_id for listing in store.listings_seen_for_search(search_id, limit=10)] == [
        "123"
    ]

    store.clear_seen_for_search(search_id)

    assert store.search_seen_count(search_id) == 0


def test_user_state_flow(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")

    assert store.get_user_state(user_id, "awaiting") is None

    store.set_user_state(user_id, "awaiting", "city")
    assert store.get_user_state(user_id, "awaiting") == "city"

    reloaded_store = ListingStore(tmp_path / "test.sqlite3")
    assert reloaded_store.get_user_state(user_id, "awaiting") == "city"

    store.set_user_state(user_id, "awaiting", "price")
    assert store.get_user_state(user_id, "awaiting") == "price"

    store.delete_user_state(user_id, "awaiting")
    assert store.get_user_state(user_id, "awaiting") is None


def test_trial_and_paid_access_flow(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")

    assert store.user_has_active_access(user_id) is False

    user = store.start_trial_if_needed(user_id, days=7)
    assert user is not None
    assert user["trial_started_at"] is not None
    assert user["trial_ends_at"] is not None
    assert store.user_has_used_trial(user_id) is True
    assert store.user_has_active_trial(user_id) is True
    assert store.user_has_active_access(user_id) is True

    same_trial = store.start_trial_if_needed(user_id, days=7)
    assert same_trial is not None
    assert same_trial["trial_started_at"] == user["trial_started_at"]

    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    with store.engine.begin() as conn:
        conn.execute(
            update(users_table)
            .where(users_table.c.id == user_id)
            .values(trial_ends_at=expired_at, subscription_status="expired")
        )
    assert store.user_has_active_access(user_id) is False
    assert store.user_has_used_trial(user_id) is True
    assert store.user_has_active_trial(user_id) is False

    paid_until = store.grant_paid_access(user_id, days=31)
    assert paid_until is not None
    assert store.user_has_active_access(user_id) is True
    assert store.user_has_active_paid_access(user_id) is True


def test_paid_access_extends_from_current_paid_until(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")

    first_paid_until = store.grant_paid_access(user_id, days=31)
    second_paid_until = store.grant_paid_access(user_id, days=31)

    assert first_paid_until is not None
    assert second_paid_until is not None
    first_date = datetime.fromisoformat(first_paid_until)
    second_date = datetime.fromisoformat(second_paid_until)
    assert second_date - first_date == timedelta(days=31)


def test_trial_usage_survives_search_reset(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    search_id = store.create_search(
        user_id=user_id,
        title="Основной поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )

    store.start_trial_if_needed(user_id, days=7)
    store.update_search(search_id, is_active=False, initialized_at=None)
    store.clear_seen_for_search(search_id)

    same_user_id = store.upsert_user(telegram_chat_id="100")
    assert same_user_id == user_id
    assert store.user_has_used_trial(user_id) is True


def test_current_search_for_user_uses_latest_and_can_deactivate_older(
    tmp_path: Path,
) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    old_search_id = store.create_search(
        user_id=user_id,
        title="Старый поиск",
        city="Казань",
        region_id="4777",
        rooms=("1",),
        min_price=None,
        max_price=None,
        rent_type="long",
        sort_by="creation_date_from_newer_to_older",
    )
    new_search_id = store.create_search(
        user_id=user_id,
        title="Новый поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )

    current = store.current_search_for_user(user_id)

    assert current is not None
    assert current["id"] == new_search_id
    assert [search["id"] for search in store.active_searches()] == [new_search_id]

    store.deactivate_other_searches_for_user(user_id, new_search_id)
    searches = store.searches_for_user(user_id)

    assert {search["id"] for search in searches if search["is_active"]} == {new_search_id}
    assert {search["id"] for search in searches if not search["is_active"]} == {old_search_id}
