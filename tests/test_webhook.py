from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cian_rent_alerts.analytics import EV_PAYMENT_SUCCEEDED, EV_WEBHOOK_ERROR
from cian_rent_alerts.config import Settings
from cian_rent_alerts.db import ListingStore
from cian_rent_alerts.webhook import process_yookassa_webhook


def test_yookassa_webhook_grants_paid_access_once(tmp_path: Path) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        database_url=None,
        dry_run=True,
        subscription_period_days=31,
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
        is_active=False,
    )
    store.create_payment(
        user_id=user_id,
        provider_payment_id="payment-1",
        status="pending",
        amount_rub=199,
        confirmation_url="https://yookassa.test/pay",
        raw_json="{}",
    )
    payload = {
        "event": "payment.succeeded",
        "object": {
            "id": "payment-1",
            "status": "succeeded",
            "paid": True,
        },
    }

    result = process_yookassa_webhook(store, settings, payload)
    repeated = process_yookassa_webhook(store, settings, payload)

    payment = store.payment_by_provider_payment_id("payment-1")
    user = store.get_user(user_id)
    search = store.current_search_for_user(user_id)
    summary = store.analytics_events_summary_since("2000-01-01T00:00:00+00:00")

    assert result.status == "processed"
    assert repeated.status == "already_processed"
    assert payment is not None
    assert payment["status"] == "succeeded"
    assert payment["paid_until"] is not None
    assert user is not None
    assert user["paid_until"] == payment["paid_until"]
    assert datetime.fromisoformat(str(user["paid_until"]))
    assert search is not None
    assert search["id"] == search_id
    assert search["is_active"] is True
    assert summary[EV_PAYMENT_SUCCEEDED] == 1


def test_yookassa_webhook_updates_non_success_status(tmp_path: Path) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        database_url=None,
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
    store.init()
    user_id = store.upsert_user(telegram_chat_id="100")
    store.create_payment(
        user_id=user_id,
        provider_payment_id="payment-1",
        status="pending",
        amount_rub=199,
        confirmation_url=None,
        raw_json="{}",
    )

    result = process_yookassa_webhook(
        store,
        settings,
        {
            "event": "payment.canceled",
            "object": {
                "id": "payment-1",
                "status": "canceled",
                "paid": False,
            },
        },
    )

    payment = store.payment_by_provider_payment_id("payment-1")

    assert result.status == "updated"
    assert payment is not None
    assert payment["status"] == "canceled"
    assert payment["paid_until"] is None


def test_yookassa_webhook_ignores_unknown_payment(tmp_path: Path) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        database_url=None,
        dry_run=True,
    )
    store = ListingStore(settings.database_path)
    store.init()

    result = process_yookassa_webhook(
        store,
        settings,
        {
            "event": "payment.succeeded",
            "object": {
                "id": "unknown-payment",
                "status": "succeeded",
                "paid": True,
            },
        },
    )

    summary = store.analytics_events_summary_since("2000-01-01T00:00:00+00:00")

    assert result.status == "unknown_payment"
    assert summary[EV_WEBHOOK_ERROR] == 1
