from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from sqlalchemy import update
from telegram.error import TelegramError

from cian_rent_alerts import service
from cian_rent_alerts.analytics import EV_CAPTCHA_ERROR, EV_TRIAL_EXPIRED
from cian_rent_alerts.config import ConfigError, Settings
from cian_rent_alerts.db import ListingStore, users_table
from cian_rent_alerts.models import Listing
from cian_rent_alerts.scraper import CaptchaError, EmptyParseError, NetworkFetchError


def test_multi_user_check_seeds_existing_listings_before_notifications(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
        listing_limit=10,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    search_id = store.create_search(
        user_id=user_id,
        title="Основной поиск",
        city="Казань",
        region_id="4777",
        rooms=("1",),
        min_price=None,
        max_price=None,
        rent_type="long",
        sort_by="creation_date_from_newer_to_older",
    )

    batches = [
        [_listing("1")],
        [_listing("1"), _listing("2")],
    ]

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: batches.pop(0))

    first_result = service.run_check(settings)
    assert first_result == 0
    assert store.search_seen_count(search_id) == 1

    second_result = service.run_check(settings)
    last_run = store.last_check_run()

    assert second_result == 0
    assert store.search_seen_count(search_id) == 2
    assert last_run is not None
    assert last_run["new_listings"] == 1


def test_empty_first_check_initializes_search_without_cooldown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
        listing_limit=10,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    search_id = store.create_search(
        user_id=user_id,
        title="Основной поиск",
        city="Казань",
        region_id="4777",
        rooms=("1",),
        min_price=None,
        max_price=None,
        rent_type="long",
        sort_by="creation_date_from_newer_to_older",
    )
    batches = [
        [],
        [_listing("2")],
    ]

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: batches.pop(0))

    assert service.run_check(settings) == 0
    initialized_search = store.first_search_for_user(user_id)
    first_run = store.last_check_run()

    assert initialized_search is not None
    assert initialized_search["initialized_at"] is not None
    assert initialized_search["last_error_type"] is None
    assert initialized_search["cooldown_until"] is None
    assert store.search_seen_count(search_id) == 0
    assert first_run is not None
    assert first_run["status"] == "success"
    assert first_run["listings_found"] == 0

    assert service.run_check(settings) == 0
    second_run = store.last_check_run()

    assert store.search_seen_count(search_id) == 1
    assert second_run is not None
    assert second_run["new_listings"] == 1


def test_identical_searches_share_single_cian_fetch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
        listing_limit=10,
    )
    store = ListingStore(settings.database_path)
    store.init()
    first_user_id = store.upsert_user(telegram_chat_id="100")
    second_user_id = store.upsert_user(telegram_chat_id="200")
    first_search_id = store.create_search(
        user_id=first_user_id,
        title="Основной поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=60000,
        rent_type="long",
        sort_by="creation_date_from_newer_to_older",
    )
    second_search_id = store.create_search(
        user_id=second_user_id,
        title="Основной поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=60000,
        rent_type="long",
        sort_by="creation_date_from_newer_to_older",
    )
    scrape_calls = 0

    def fake_scrape(_scraper, _limit):
        nonlocal scrape_calls
        scrape_calls += 1
        return [_listing("1")]

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", fake_scrape)

    assert service.run_check(settings) == 0
    last_run = store.last_check_run()

    assert scrape_calls == 1
    assert store.search_seen_count(first_search_id) == 1
    assert store.search_seen_count(second_search_id) == 1
    assert last_run is not None
    assert last_run["active_searches"] == 2
    assert last_run["unique_search_groups"] == 1
    assert last_run["cian_fetches"] == 1
    assert last_run["shared_group_hits"] == 1


def test_check_for_chat_without_active_search_does_not_scrape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    store.create_search(
        user_id=user_id,
        title="Основной поиск",
        city="Казань",
        region_id="4777",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
        is_active=False,
    )

    monkeypatch.setattr(
        service,
        "build_scraper",
        lambda _settings: (_ for _ in ()).throw(AssertionError("should not scrape")),
    )

    assert service.run_check(settings, only_chat_id="100") == 0


def test_check_waits_between_multiple_searches(tmp_path: Path, monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
        search_check_delay_seconds=5,
    )
    store = ListingStore(settings.database_path)
    store.init()
    first_user_id = store.upsert_user(telegram_chat_id="100")
    second_user_id = store.upsert_user(telegram_chat_id="200")
    for user_id, city, region_id in (
        (first_user_id, "Москва", "1"),
        (second_user_id, "Казань", "4777"),
    ):
        store.create_search(
            user_id=user_id,
            title="Основной поиск",
            city=city,
            region_id=region_id,
            rooms=("all",),
            min_price=None,
            max_price=None,
            rent_type="all",
            sort_by="creation_date_from_newer_to_older",
        )

    sleeps: list[int] = []
    monkeypatch.setattr(service.time, "sleep", sleeps.append)
    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("1")])

    assert service.run_check(settings) == 0
    assert sleeps == [5]


def test_manual_check_does_not_wait_between_searches(tmp_path: Path, monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
        search_check_delay_seconds=5,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
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
    )

    sleeps: list[int] = []
    monkeypatch.setattr(service.time, "sleep", sleeps.append)
    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("1")])

    assert service.run_check(settings, only_chat_id="100") == 0
    assert sleeps == []


def test_manual_check_can_target_one_search_for_chat(tmp_path: Path, monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
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
    seen_urls: list[str] = []

    def fake_scrape(scraper, _limit):
        seen_urls.append(scraper.config.search_url)
        return [_listing("2")]

    monkeypatch.setattr(service, "scrape", fake_scrape)

    result = service.run_check_result(
        settings,
        only_chat_id="100",
        only_search_id=new_search_id,
    )

    assert result.notifications_sent == 0
    assert store.search_seen_count(old_search_id) == 0
    assert store.search_seen_count(new_search_id) == 1
    assert len(seen_urls) == 1
    assert "region=1" in seen_urls[0]


def test_global_check_does_not_fallback_to_app_settings_when_user_searches_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        telegram_chat_id="primary-chat",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    store.create_search(
        user_id=user_id,
        title="Остановленный поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
        is_active=False,
    )
    store.set_runtime_setting("cian_city", "Казань")
    store.set_runtime_setting("cian_region_id", "4777")

    monkeypatch.setattr(
        service,
        "build_scraper",
        lambda _settings: (_ for _ in ()).throw(AssertionError("should not scrape")),
    )

    assert service.run_check(settings) == 0


def test_scheduled_check_does_not_use_global_fallback_without_user_searches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        telegram_chat_id="primary-chat",
        dry_run=False,
        telegram_bot_token="token",
    )
    store = ListingStore(settings.database_path)
    store.init()

    monkeypatch.setattr(
        service,
        "build_scraper",
        lambda _settings: (_ for _ in ()).throw(AssertionError("should not scrape")),
    )

    result = service.run_check_result(settings)
    last_run = store.last_check_run()

    assert result.notifications_sent == 0
    assert last_run is not None
    assert last_run["listings_found"] == 0
    assert last_run["notifications_sent"] == 0


def test_explicit_global_fallback_still_supports_once_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        telegram_chat_id="primary-chat",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
    store.init()

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("1")])

    result = service.run_check_result(settings, allow_global_fallback=True)
    last_run = store.last_check_run()

    assert result.notifications_sent == 1
    assert last_run is not None
    assert last_run["listings_found"] == 1


def test_scrape_with_fallback_retries_playwright_on_empty_parse(monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        playwright_fallback=True,
        use_playwright=False,
        listing_limit=10,
    )
    expected = [_listing("1")]
    calls: list[str] = []

    monkeypatch.setattr(service, "build_scraper", lambda _settings: "requests")
    monkeypatch.setattr(service, "build_playwright_scraper", lambda _settings: "playwright")

    def fake_scrape(scraper, _limit):
        calls.append(scraper)
        if scraper == "requests":
            raise EmptyParseError("No listings found on the page")
        return expected

    monkeypatch.setattr(service, "scrape", fake_scrape)

    assert service.scrape_with_fallback(settings) == expected
    assert calls == ["requests", "playwright"]


def test_scrape_with_fallback_stays_disabled_by_default(monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        playwright_fallback=False,
        use_playwright=False,
    )

    monkeypatch.setattr(service, "build_scraper", lambda _settings: "requests")
    monkeypatch.setattr(
        service,
        "build_playwright_scraper",
        lambda _settings: (_ for _ in ()).throw(AssertionError("should not fallback")),
    )
    monkeypatch.setattr(
        service,
        "scrape",
        lambda _scraper, _limit: (_ for _ in ()).throw(CaptchaError("blocked")),
    )

    try:
        service.scrape_with_fallback(settings)
    except CaptchaError:
        pass
    else:
        raise AssertionError("Expected CaptchaError")


def test_scrape_with_fallback_preserves_original_error_when_playwright_missing(
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        playwright_fallback=True,
        use_playwright=False,
    )

    monkeypatch.setattr(service, "build_scraper", lambda _settings: "requests")
    monkeypatch.setattr(service, "build_playwright_scraper", lambda _settings: "playwright")

    def fake_scrape(scraper, _limit):
        if scraper == "requests":
            raise CaptchaError("blocked")
        raise RuntimeError("Playwright is not installed")

    monkeypatch.setattr(service, "scrape", fake_scrape)

    try:
        service.scrape_with_fallback(settings)
    except CaptchaError as exc:
        assert isinstance(exc.__cause__, RuntimeError)
    else:
        raise AssertionError("Expected CaptchaError")


def test_scrape_with_retries_recovers_from_network_error(monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        parser_retry_attempts=3,
        parser_retry_backoff_seconds=0,
    )
    expected = [_listing("1")]
    calls = 0

    def fake_scrape_with_fallback(_settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NetworkFetchError("temporary timeout")
        return expected

    monkeypatch.setattr(service, "scrape_with_fallback", fake_scrape_with_fallback)

    assert service.scrape_with_retries(settings) == expected
    assert calls == 2


def test_scrape_with_retries_does_not_retry_empty_parse(monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        parser_retry_attempts=3,
        parser_retry_backoff_seconds=0,
    )
    calls = 0

    def fake_scrape_with_fallback(_settings):
        nonlocal calls
        calls += 1
        raise EmptyParseError("No listings found on the page")

    monkeypatch.setattr(service, "scrape_with_fallback", fake_scrape_with_fallback)

    try:
        service.scrape_with_retries(settings)
    except EmptyParseError:
        pass
    else:
        raise AssertionError("Expected EmptyParseError")

    assert calls == 1


def test_scrape_with_retries_stops_after_configured_attempts(monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        parser_retry_attempts=2,
        parser_retry_backoff_seconds=0,
    )
    calls = 0

    def fake_scrape_with_fallback(_settings):
        nonlocal calls
        calls += 1
        raise NetworkFetchError("temporary timeout")

    monkeypatch.setattr(service, "scrape_with_fallback", fake_scrape_with_fallback)

    try:
        service.scrape_with_retries(settings)
    except NetworkFetchError:
        pass
    else:
        raise AssertionError("Expected NetworkFetchError")

    assert calls == 2


def test_one_failed_search_does_not_stop_other_searches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
    store.init()
    failed_user_id = store.upsert_user(telegram_chat_id="100")
    successful_user_id = store.upsert_user(telegram_chat_id="200")
    failed_search_id = store.create_search(
        user_id=failed_user_id,
        title="Падающий поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )
    successful_search_id = store.create_search(
        user_id=successful_user_id,
        title="Рабочий поиск",
        city="Казань",
        region_id="4777",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )

    def fake_scrape(_scraper, _limit):
        if _scraper == "Москва":
            raise RuntimeError("captcha")
        return [_listing("200")]

    monkeypatch.setattr(service, "build_scraper", lambda search_settings: search_settings.cian_city)
    monkeypatch.setattr(service, "scrape", fake_scrape)

    assert service.run_check(settings) == 0
    last_run = store.last_check_run()

    assert store.search_seen_count(failed_search_id) == 0
    assert store.search_seen_count(successful_search_id) == 1
    assert last_run is not None
    assert last_run["status"] == "partial"
    assert "captcha:" in str(last_run["error"])
    assert "search_id=" in str(last_run["error"])
    assert "captcha" in str(last_run["error"])


def test_failed_search_gets_cooldown_and_is_skipped_next_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
        parser_problem_cooldown_seconds=3600,
        parser_network_cooldown_seconds=900,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    search_id = store.create_search(
        user_id=user_id,
        title="Падающий поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(
        service,
        "scrape",
        lambda _scraper, _limit: (_ for _ in ()).throw(CaptchaError("blocked")),
    )

    assert service.run_check(settings) == 0
    search = store.first_search_for_user(user_id)
    assert search is not None
    assert search["last_error_type"] == "captcha"
    assert search["last_error_at"] is not None
    assert search["cooldown_until"] is not None
    assert store.cooldown_searches_count() == 1
    assert store.active_searches() == []
    assert store.analytics_events_summary_since("2000-01-01T00:00:00+00:00")[EV_CAPTCHA_ERROR] == 1

    monkeypatch.setattr(
        service,
        "scrape",
        lambda _scraper, _limit: (_ for _ in ()).throw(AssertionError("should not scrape")),
    )

    assert service.run_check(settings) == 0
    assert store.search_seen_count(search_id) == 0


def test_expired_trial_event_is_recorded_once(tmp_path: Path, monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
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
    with store.engine.begin() as conn:
        conn.execute(
            update(users_table)
            .where(users_table.c.id == user_id)
            .values(trial_ends_at="2026-01-01T00:00:00+00:00")
        )

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("1")])

    assert service.run_check(settings) == 0
    assert service.run_check(settings) == 0

    summary = store.analytics_events_summary_since("2000-01-01T00:00:00+00:00")
    assert summary[EV_TRIAL_EXPIRED] == 1
    assert store.search_seen_count(search_id) == 1


def test_active_trial_does_not_record_expired_event(tmp_path: Path, monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
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

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("1")])

    assert service.run_check(settings) == 0

    summary = store.analytics_events_summary_since("2000-01-01T00:00:00+00:00")
    assert summary.get(EV_TRIAL_EXPIRED, 0) == 0
    assert store.search_seen_count(search_id) == 1


def test_successful_search_clears_previous_cooldown(tmp_path: Path, monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    search_id = store.create_search(
        user_id=user_id,
        title="Восстановленный поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )
    store.record_search_error(
        search_id,
        error_type="network",
        cooldown_until=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds"),
    )

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("200")])

    assert service.run_check(settings) == 0
    search = store.first_search_for_user(user_id)

    assert search is not None
    assert search["last_error_type"] is None
    assert search["last_error_at"] is None
    assert search["cooldown_until"] is None


def test_partial_check_notifies_admins_for_actionable_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=False,
        telegram_bot_token="token",
        admin_telegram_ids=frozenset({"900", "901"}),
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    store.create_search(
        user_id=user_id,
        title="Падающий поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )
    sent_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(
        service,
        "scrape",
        lambda _scraper, _limit: (_ for _ in ()).throw(NetworkFetchError("timeout")),
    )
    monkeypatch.setattr(
        service,
        "send_message_sync",
        lambda notifier, text: sent_messages.append((notifier.chat_id, text)),
    )

    assert service.run_check(settings) == 0

    assert {chat_id for chat_id, _text in sent_messages} == {"900", "901"}
    assert all("Status: partial" in text for _chat_id, text in sent_messages)
    assert all("network" in text for _chat_id, text in sent_messages)


def test_partial_check_does_not_push_admins_for_expected_parser_noise(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=False,
        telegram_bot_token="token",
        admin_telegram_ids=frozenset({"900"}),
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    store.create_search(
        user_id=user_id,
        title="Падающий поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )
    sent_messages: list[str] = []

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(
        service,
        "scrape",
        lambda _scraper, _limit: (_ for _ in ()).throw(EmptyParseError("empty")),
    )
    monkeypatch.setattr(
        service,
        "send_message_sync",
        lambda _notifier, text: sent_messages.append(text),
    )

    assert service.run_check(settings) == 0

    assert sent_messages == []
    assert store.recent_check_runs(limit=1)[0]["status"] == "partial"


def test_partial_check_does_not_notify_admins_in_dry_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
        telegram_bot_token="token",
        admin_telegram_ids=frozenset({"900"}),
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    store.create_search(
        user_id=user_id,
        title="Падающий поиск",
        city="Москва",
        region_id="1",
        rooms=("all",),
        min_price=None,
        max_price=None,
        rent_type="all",
        sort_by="creation_date_from_newer_to_older",
    )
    sent_messages: list[str] = []

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(
        service,
        "scrape",
        lambda _scraper, _limit: (_ for _ in ()).throw(RuntimeError("captcha")),
    )
    monkeypatch.setattr(
        service,
        "send_message_sync",
        lambda _notifier, text: sent_messages.append(text),
    )

    assert service.run_check(settings) == 0

    assert sent_messages == []


def test_no_new_listings_nudge_is_sent_once_per_day(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=False,
        telegram_bot_token="token",
    )
    store = ListingStore(settings.database_path)
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
    batches = [
        [_listing("1")],
        [_listing("1")],
        [_listing("1")],
    ]
    sent_messages: list[tuple[str, object]] = []

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: batches.pop(0))
    monkeypatch.setattr(
        service,
        "send_message_sync",
        lambda _notifier, text, reply_markup=None: sent_messages.append((text, reply_markup)),
    )

    assert service.run_check(settings) == 0
    store.set_user_state(
        user_id,
        service._last_new_listing_state_key(search_id),
        (datetime.now(UTC) - timedelta(hours=25)).isoformat(timespec="seconds"),
    )

    assert service.run_check(settings) == 0
    assert service.run_check(settings) == 0

    assert len(sent_messages) == 1
    text, reply_markup = sent_messages[0]
    assert "За последние 24 часа" in text
    assert "расширить поиск" in text
    assert reply_markup is not None
    assert reply_markup.inline_keyboard[0][0].text == "⚙️ Настроить фильтры"


def test_active_trial_allows_new_listing_notifications(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=False,
        telegram_bot_token="token",
    )
    store = ListingStore(settings.database_path)
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
    store.upsert_many([_listing("1")])
    store.mark_search_listing_seen(search_id=search_id, cian_id="1", sent=False)
    store.update_search(search_id, initialized_at=datetime.now(UTC).isoformat(timespec="seconds"))
    store.start_trial_if_needed(user_id, days=7)
    sent_batches: list[list[str]] = []

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("1"), _listing("2")])
    monkeypatch.setattr(
        service,
        "send_listings_sync",
        lambda _notifier, listings: (
            sent_batches.append([item.cian_id for item in listings])
            or [item.cian_id for item in listings]
        ),
    )

    assert service.run_check(settings) == 1

    assert sent_batches == [["2"]]
    assert store.search_seen_count(search_id) == 2


def test_expired_access_blocks_notifications_without_marking_seen(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=False,
        telegram_bot_token="token",
    )
    store = ListingStore(settings.database_path)
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
    store.upsert_many([_listing("1")])
    store.mark_search_listing_seen(search_id=search_id, cian_id="1", sent=False)
    store.update_search(search_id, initialized_at=datetime.now(UTC).isoformat(timespec="seconds"))
    sent_messages: list[tuple[str, object]] = []

    monkeypatch.setattr(service, "build_scraper", lambda _settings: object())
    monkeypatch.setattr(service, "scrape", lambda _scraper, _limit: [_listing("1"), _listing("2")])
    monkeypatch.setattr(
        service,
        "send_listings_sync",
        lambda _notifier, _listings: (_ for _ in ()).throw(
            AssertionError("should not send listings")
        ),
    )
    monkeypatch.setattr(
        service,
        "send_message_sync",
        lambda _notifier, text, reply_markup=None: sent_messages.append((text, reply_markup)),
    )

    assert service.run_check(settings) == 0

    assert store.search_seen_count(search_id) == 1
    assert len(sent_messages) == 1
    text, reply_markup = sent_messages[0]
    assert "Пробный период закончился" in text
    assert reply_markup.inline_keyboard[0][0].callback_data == "cfg:subscribe"


def test_run_check_skips_when_another_check_is_running(tmp_path: Path) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )

    acquired = service._CHECK_LOCK.acquire(blocking=False)
    assert acquired
    try:
        assert service.run_check(settings) == 0
    finally:
        service._CHECK_LOCK.release()


def test_run_check_can_fail_when_another_check_is_running(tmp_path: Path) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        dry_run=True,
    )

    acquired = service._CHECK_LOCK.acquire(blocking=False)
    assert acquired
    try:
        try:
            service.run_check(settings, fail_if_running=True)
        except service.CheckAlreadyRunning:
            pass
        else:
            raise AssertionError("Expected CheckAlreadyRunning")
    finally:
        service._CHECK_LOCK.release()


def test_classify_check_error() -> None:
    network_error = RuntimeError("Failed to fetch CIAN search page")
    network_error.__cause__ = requests.Timeout("timeout")

    assert service.classify_check_error(RuntimeError("captcha/access-check page")) == "captcha"
    assert (
        service.classify_check_error(RuntimeError("No listings found on the page")) == "empty_parse"
    )
    assert service.classify_check_error(network_error) == "network"
    assert service.classify_check_error(ConfigError("bad settings")) == "config"
    assert service.classify_check_error(TelegramError("chat not found")) == "telegram"
    assert service.classify_check_error(CaptchaError("blocked")) == "captcha"
    assert service.classify_check_error(EmptyParseError("empty")) == "empty_parse"
    assert service.classify_check_error(NetworkFetchError("timeout")) == "network"
    assert service.classify_check_error(RuntimeError("something else")) == "unknown"


def _listing(cian_id: str) -> Listing:
    return Listing(
        cian_id=cian_id,
        url=f"https://www.cian.ru/rent/flat/{cian_id}/",
        title="1-комн. квартира",
    )
