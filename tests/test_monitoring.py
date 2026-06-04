from dataclasses import replace
from pathlib import Path

from cian_rent_alerts.analytics import EV_WEBHOOK_ERROR
from cian_rent_alerts.config import Settings
from cian_rent_alerts.db import ListingStore
from cian_rent_alerts import service


def test_run_monitoring_alerts_when_no_successful_check(tmp_path: Path, monkeypatch) -> None:
    settings = _monitoring_settings(tmp_path)
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
        is_active=True,
    )
    messages: list[str] = []
    monkeypatch.setattr(service, "send_message_sync", lambda _notifier, text: messages.append(text))

    service.run_monitoring(settings)
    service.run_monitoring(settings)

    assert len(messages) == 1
    assert "FlatPulse: мониторинг" in messages[0]
    assert "нет успешных проверок" in messages[0]


def test_run_monitoring_alerts_for_webhook_errors(tmp_path: Path, monkeypatch) -> None:
    settings = _monitoring_settings(tmp_path)
    store = ListingStore(settings.database_path)
    store.init()
    store.record_event(EV_WEBHOOK_ERROR, metadata={"reason": "bad_request"})
    messages: list[str] = []
    monkeypatch.setattr(service, "send_message_sync", lambda _notifier, text: messages.append(text))

    service.run_monitoring(settings)

    assert len(messages) == 1
    assert "webhook_errors за 24ч: 1" in messages[0]


def _monitoring_settings(tmp_path: Path) -> Settings:
    return replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        database_url=None,
        dry_run=False,
        telegram_bot_token="test-token",
        admin_telegram_ids=frozenset({"999"}),
        monitoring_alert_cooldown_seconds=3600,
        monitoring_webhook_error_threshold=1,
    )
