from pathlib import Path

from cian_rent_alerts.db import ListingStore
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
