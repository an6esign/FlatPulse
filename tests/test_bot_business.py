import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import update

from cian_rent_alerts import bot as bot_module
from cian_rent_alerts.billing import PaymentStatus
from cian_rent_alerts.bot import SettingsBot
from cian_rent_alerts.config import Settings
from cian_rent_alerts.db import users_table


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, object]] = []

    async def reply_text(
        self,
        text: str,
        *,
        reply_markup: object = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        self.replies.append((text, reply_markup))


class FakeUpdate:
    def __init__(self, chat_id: int = 100) -> None:
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(id=200, username="tester")
        self.effective_message = FakeMessage()
        self.callback_query = None


def _settings(tmp_path) -> Settings:
    return replace(
        Settings.from_env(env_file=None),
        database_path=tmp_path / "test.sqlite3",
        database_url=None,
        telegram_bot_token="test-token",
    )


def test_start_ignores_immediate_duplicate(tmp_path) -> None:
    bot = SettingsBot(_settings(tmp_path))
    update_obj = FakeUpdate()
    context = SimpleNamespace()

    asyncio.run(bot.start(update_obj, context))
    asyncio.run(bot.start(update_obj, context))

    assert len(update_obj.effective_message.replies) == 1
    assert "Найдите квартиру раньше других" in update_obj.effective_message.replies[0][0]


def test_access_status_shows_trial_and_stopped_search(tmp_path) -> None:
    bot = SettingsBot(_settings(tmp_path))
    update_obj = FakeUpdate()
    search = bot._ensure_user_search(update_obj)
    assert search is not None
    user_id = int(search["user_id"])

    bot.store.start_trial_if_needed(user_id, days=7)
    bot.store.update_search(int(search["id"]), is_active=True)

    assert "Пробный период активен" in bot.access_status_for_update(update_obj)

    bot.store.update_search(int(search["id"]), is_active=False)

    assert bot.access_status_for_update(update_obj) == "⏸ Уведомления остановлены"


def test_start_trial_activates_search_once(tmp_path) -> None:
    bot = SettingsBot(_settings(tmp_path))
    update_obj = FakeUpdate()

    search = bot._ensure_user_search(update_obj)
    assert search is not None

    asyncio.run(bot._start_trial(update_obj))

    current_search = bot.store.current_search_for_user(int(search["user_id"]))
    assert current_search is not None
    assert current_search["is_active"] is True
    assert "Бесплатный период запущен" in update_obj.effective_message.replies[-1][0]


def test_expired_trial_cannot_be_started_again_or_activate_search(tmp_path) -> None:
    bot = SettingsBot(_settings(tmp_path))
    update_obj = FakeUpdate()
    search = bot._ensure_user_search(update_obj)
    assert search is not None
    user_id = int(search["user_id"])

    bot.store.start_trial_if_needed(user_id, days=7)
    expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    with bot.store.engine.begin() as conn:
        conn.execute(
            update(users_table)
            .where(users_table.c.id == user_id)
            .values(trial_ends_at=expired_at, subscription_status="expired")
        )

    asyncio.run(bot._start_trial(update_obj))

    current_search = bot.store.current_search_for_user(user_id)
    assert current_search is not None
    assert current_search["is_active"] is False
    assert "Бесплатный период уже был использован" in update_obj.effective_message.replies[-1][0]


def test_pending_payment_is_visible_in_access_status(tmp_path) -> None:
    bot = SettingsBot(_settings(tmp_path))
    update_obj = FakeUpdate()
    search = bot._ensure_user_search(update_obj)
    assert search is not None
    user_id = int(search["user_id"])
    bot.store.create_payment(
        user_id=user_id,
        provider_payment_id="payment-1",
        status="pending",
        amount_rub=199,
        confirmation_url="https://yookassa.test/pay",
        raw_json="{}",
    )

    assert bot.access_status_for_update(update_obj) == "💳 Ожидает оплаты"


def test_manual_payment_check_activates_search(tmp_path, monkeypatch) -> None:
    settings = replace(
        _settings(tmp_path),
        yookassa_shop_id="shop",
        yookassa_secret_key="secret",
        yookassa_return_url="https://flatpulse.ru/thanks",
    )
    bot = SettingsBot(settings)
    update_obj = FakeUpdate()
    search = bot._ensure_user_search(update_obj)
    assert search is not None
    user_id = int(search["user_id"])
    bot.store.update_search(
        int(search["id"]),
        is_active=False,
        initialized_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds"),
    )
    bot.store.create_payment(
        user_id=user_id,
        provider_payment_id="payment-1",
        status="pending",
        amount_rub=199,
        confirmation_url="https://yookassa.test/pay",
        raw_json="{}",
    )

    class FakeYooKassaClient:
        def __init__(self, _settings: Settings) -> None:
            pass

        def get_payment(self, provider_payment_id: str) -> PaymentStatus:
            return PaymentStatus(
                provider_payment_id=provider_payment_id,
                status="succeeded",
                paid=True,
                raw_json="{}",
            )

    monkeypatch.setattr(bot_module, "YooKassaClient", FakeYooKassaClient)

    asyncio.run(bot._check_subscription_payment(update_obj))

    current_search = bot.store.current_search_for_user(user_id)
    assert current_search is not None
    assert current_search["is_active"] is True
    assert current_search["initialized_at"] is None
    assert "Уведомления включены" in update_obj.effective_message.replies[-1][0]
