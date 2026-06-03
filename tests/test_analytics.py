from pathlib import Path

from cian_rent_alerts.analytics import EV_START, EV_TRIAL_STARTED
from cian_rent_alerts.db import ListingStore


def test_analytics_events_summary(tmp_path: Path) -> None:
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

    store.record_event(EV_START, user_id=user_id, search_id=search_id)
    store.record_event(EV_TRIAL_STARTED, user_id=user_id, search_id=search_id)
    store.record_event(EV_TRIAL_STARTED, user_id=user_id, search_id=search_id)

    summary = store.analytics_events_summary_since("2000-01-01T00:00:00+00:00")

    assert summary[EV_START] == 1
    assert summary[EV_TRIAL_STARTED] == 2
