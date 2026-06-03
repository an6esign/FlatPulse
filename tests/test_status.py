from pathlib import Path
from dataclasses import replace

from cian_rent_alerts.bot import (
    _format_admin_health,
    _format_admin_metrics,
    _format_admin_report,
    _format_admin_searches,
    _format_admin_status,
    _format_admin_users,
    _format_check_runs,
)
from cian_rent_alerts.config import Settings
from cian_rent_alerts.db import ListingStore
from cian_rent_alerts.analytics import EV_MANUAL_CHECK, EV_TRIAL_STARTED


def test_store_last_check_run(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()

    run_id = store.start_check_run()
    store.finish_check_run(
        run_id,
        status="success",
        listings_found=10,
        listings_saved=10,
        new_listings=2,
        notifications_sent=2,
        active_searches=4,
        unique_search_groups=2,
        cian_fetches=2,
        shared_group_hits=2,
    )

    last_run = store.last_check_run()

    assert last_run is not None
    assert last_run["status"] == "success"
    assert last_run["listings_found"] == 10
    assert last_run["new_listings"] == 2
    assert last_run["notifications_sent"] == 2
    assert last_run["active_searches"] == 4
    assert last_run["unique_search_groups"] == 2
    assert last_run["cian_fetches"] == 2
    assert last_run["shared_group_hits"] == 2


def test_recent_failed_check_runs(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()

    run_id = store.start_check_run()
    store.finish_check_run(run_id, status="failed", error="captcha")

    failed_runs = store.recent_failed_check_runs()

    assert len(failed_runs) == 1
    assert failed_runs[0]["status"] == "failed"
    assert failed_runs[0]["error"] == "captcha"


def test_user_state_persists_between_store_instances(tmp_path: Path) -> None:
    database_path = tmp_path / "test.sqlite3"
    store = ListingStore(database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    store.set_user_state(user_id, "last_manual_check_at", "2026-05-31T10:00:00+00:00")

    reopened_store = ListingStore(database_path)

    assert (
        reopened_store.get_user_state(user_id, "last_manual_check_at")
        == "2026-05-31T10:00:00+00:00"
    )


def test_format_admin_status_without_runs(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()
    settings = Settings.from_env(env_file=None)

    status = _format_admin_status(store, settings)

    assert "Admin status" in status
    assert "Last check: none" in status
    assert "Users: 0 total, 0 active" in status
    assert "Searches: 0 total, 0 active, 0 stopped, 0 cooldown" in status


def test_format_check_runs() -> None:
    text = _format_check_runs(
        "Последние проверки",
        [
            {
                "id": 1,
                "started_at": "2026-05-30T10:00:00+00:00",
                "finished_at": "2026-05-30T10:00:05+00:00",
                "status": "success",
                "listings_found": 10,
                "new_listings": 2,
                "notifications_sent": 2,
                "error": None,
            }
        ],
    )

    assert "Последние проверки" in text
    assert "success" in text
    assert "found=10" in text


def test_admin_monitoring_counts_and_formatters(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100", username="tester", is_admin=True)
    store.create_search(
        user_id=user_id,
        title="Основной поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
        is_active=False,
    )
    run_id = store.start_check_run()
    store.finish_check_run(run_id, status="failed", error="captcha")
    partial_run_id = store.start_check_run()
    store.finish_check_run(partial_run_id, status="partial", error="one search failed")

    assert store.users_count() == 1
    assert store.users_count(active=True) == 1
    assert store.searches_count_by_status() == 1
    assert store.searches_count_by_status(active=False) == 1
    assert store.check_runs_summary_since("2000-01-01T00:00:00+00:00")["failed_runs"] == 1
    assert store.check_runs_summary_since("2000-01-01T00:00:00+00:00")["partial_runs"] == 1

    users_text = _format_admin_users(store.recent_users())
    searches_text = _format_admin_searches(store.recent_searches())
    health_settings = replace(
        Settings.from_env(env_file=None),
        playwright_fallback=True,
        parser_retry_attempts=3,
        parser_retry_backoff_seconds=2,
        search_check_delay_seconds=5,
        parser_problem_cooldown_seconds=3600,
        parser_network_cooldown_seconds=900,
    )
    health_text = _format_admin_health(store, health_settings)
    status_text = _format_admin_status(store, health_settings)
    report_text = _format_admin_report(store, health_settings)

    assert "chat=100" in users_text
    assert "@tester" in users_text
    assert "stopped" in searches_text
    assert "Москва" in searches_text
    assert "Admin health" in health_text
    assert "DB: ok" in health_text
    assert "Last groups:" in health_text
    assert "Groups:" in status_text
    assert "Health: degraded" in health_text
    assert "partial=1" in health_text
    assert "Parser: requests+playwright_fallback, retry=3 backoff=2 sec" in health_text
    assert "search_delay=5 sec" in health_text
    assert "problem_cooldown=60 min" in health_text
    assert "network_cooldown=15 min" in health_text
    assert "Admin health" in report_text
    assert "Admin status" in report_text
    assert "Admin searches" in report_text
    assert "Последние проверки" in report_text
    assert "Последние ошибки" in report_text


def test_format_admin_metrics(tmp_path: Path) -> None:
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
    run_id = store.start_check_run()
    store.finish_check_run(
        run_id,
        status="success",
        listings_found=12,
        new_listings=3,
        notifications_sent=3,
        active_searches=2,
        unique_search_groups=1,
        cian_fetches=1,
        shared_group_hits=1,
    )
    store.record_event(EV_MANUAL_CHECK, user_id=user_id, search_id=search_id)
    store.record_event(EV_TRIAL_STARTED, user_id=user_id, search_id=search_id)

    text = _format_admin_metrics(store)

    assert "Metrics 24h" in text
    assert "users_total: 1" in text
    assert "searches_active: 1" in text
    assert "trial_started: 1" in text
    assert "manual_checks: 1" in text
    assert "listings_found: 12" in text
    assert "new_listings_sent: 3" in text
    assert "groups: active=2 unique=1 fetches=1 shared=1" in text
    assert "/start: 0" in text


def test_schema_version_is_unknown_without_alembic_table(tmp_path: Path) -> None:
    store = ListingStore(tmp_path / "test.sqlite3")
    store.init()

    assert store.schema_version() is None
