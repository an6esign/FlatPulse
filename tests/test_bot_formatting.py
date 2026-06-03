from datetime import UTC, datetime

from cian_rent_alerts.bot import (
    SHOW_FOUND_LIMIT,
    _default_user_search_values,
    _dev_payment_screen_keyboard,
    _dev_payment_screen_text,
    _empty_archive_text,
    _first_entry_text,
    _format_area,
    _format_radius_label,
    _format_settings,
    _help_text,
    _initial_seed_text,
    _main_keyboard,
    _manual_check_cooldown_remaining,
    _parse_manual_city,
    _parse_manual_price,
    _parse_manual_rent,
    _parse_manual_rooms,
    _start_reply_keyboard,
    _stopped_search_keyboard,
    _trial_offer_keyboard,
    _trial_offer_text,
    _trial_used_text,
    _welcome_text,
    _show_found_keyboard,
    _search_settings_keyboard,
)
from cian_rent_alerts.config import ConfigError, Settings


def test_format_radius_label_is_stable_for_settings_display() -> None:
    label = _format_radius_label("Казань, Калинина 23", 1000)

    assert label == "радиус 1000 м от: Казань, Калинина 23"
    assert _format_area("49.1_55.7,49.2_55.7", label) == label


def test_format_area_has_clear_fallback_for_legacy_polygon() -> None:
    assert _format_area("49.1_55.7,49.2_55.7", None) == "область задана"
    assert _format_area(None, None) == "Любая"


def test_format_area_normalizes_legacy_radius_label() -> None:
    assert (
        _format_area("49.1_55.7,49.2_55.7", "Казань, Калинина 23, 1000 м")
        == "радиус 1000 м от: Казань, Калинина 23"
    )


def test_default_user_search_starts_with_moscow_and_any_filters() -> None:
    values = _default_user_search_values(Settings.from_env(env_file=None))

    assert values["city"] == "Москва"
    assert values["region_id"] == "1"
    assert values["rooms"] == ("all",)
    assert values["min_price"] is None
    assert values["max_price"] is None
    assert values["rent_type"] == "all"
    assert values["polygon"] is None


def test_show_found_limit_is_10() -> None:
    assert SHOW_FOUND_LIMIT == 10


def test_main_keyboard_starts_with_clear_user_actions() -> None:
    keyboard = _main_keyboard().inline_keyboard

    assert keyboard[0][0].text == "🎯 Настроить фильтры"
    assert keyboard[0][0].callback_data == "cfg:setup"
    assert keyboard[1][0].text == "⚡ Проверить новые квартиры"
    assert keyboard[2][0].text == "📦 Архив квартир"
    assert keyboard[2][0].callback_data == "cfg:archive"
    assert keyboard[3][0].text == "⚙️ Мой поиск"
    assert keyboard[3][1].text == "❓ Как это работает"


def test_stopped_search_keyboard_has_resume_action() -> None:
    keyboard = _stopped_search_keyboard().inline_keyboard

    assert keyboard[0][0].text == "Возобновить мониторинг"
    assert keyboard[0][0].callback_data == "cfg:resume"


def test_search_settings_keyboard_has_pause_and_delete_actions() -> None:
    keyboard = _search_settings_keyboard().inline_keyboard

    assert keyboard[0][0].text == "✏️ Изменить фильтры"
    assert keyboard[1][0].text == "💎 Подписка"
    assert keyboard[1][0].callback_data == "cfg:subscribe"
    assert keyboard[2][0].text == "💬 Связаться с поддержкой"
    assert keyboard[2][0].url == "https://t.me/FlatPulseSupport"
    assert keyboard[3][0].text == "⏸ Остановить уведомления"
    assert keyboard[3][0].callback_data == "cfg:stop"
    assert keyboard[4][0].text == "🗑 Удалить поиск"
    assert keyboard[4][0].callback_data == "cfg:delete"


def test_show_found_keyboard_uses_user_facing_copy() -> None:
    keyboard = _show_found_keyboard().inline_keyboard

    assert keyboard[0][0].text == "👀 Показать 10 квартир"
    assert keyboard[0][0].callback_data == "cfg:show_found"
    assert keyboard[1][0].text == "Посмотреть позже"
    assert keyboard[1][0].callback_data == "cfg:show_later"


def test_trial_offer_uses_configured_price_and_days() -> None:
    settings = Settings.from_env(env_file=None)
    text = _trial_offer_text(settings)
    keyboard = _trial_offer_keyboard(settings).inline_keyboard

    assert "0 ₽" in text
    assert "7 дней" in text
    assert "199 ₽" in text
    assert "Без автосписаний" in text
    assert keyboard[0][0].text == "Попробовать 7 дней бесплатно"
    assert keyboard[0][0].callback_data == "cfg:start_trial"
    assert keyboard[1][0].callback_data == "cfg:decline_trial"


def test_trial_used_text_points_to_manual_payment() -> None:
    text = _trial_used_text(Settings.from_env(env_file=None))

    assert "Бесплатный период уже был использован" in text
    assert "оплатите следующий месяц вручную" in text
    assert "199 ₽" in text
    assert "Без автосписаний" in text


def test_dev_payment_screen_has_required_yookassa_moderation_content() -> None:
    settings = Settings.from_env(env_file=None)
    text = _dev_payment_screen_text(settings)
    keyboard = _dev_payment_screen_keyboard().inline_keyboard

    assert "FlatPulse" in text
    assert "Подписка" in text
    assert "199 ₽" in text
    assert "без автосписаний" in text.lower()
    assert keyboard[0][0].text == "Оформить заказ"
    assert keyboard[0][0].callback_data == "cfg:subscribe"


def test_format_settings_can_show_search_status() -> None:
    settings = Settings.from_env(env_file=None)

    text = _format_settings(
        settings,
        status="остановлен",
        access_status="⏸ Уведомления остановлены",
    )

    assert "Поиск: остановлен" in text
    assert "⏸ Уведомления остановлены" in text
    assert "Новые объявления сейчас не приходят" in text


def test_empty_archive_text_explains_next_step() -> None:
    uninitialized = _empty_archive_text({"initialized_at": None})
    initialized = _empty_archive_text({"initialized_at": "2026-06-03T10:00:00+00:00"})

    assert "Архив пока пуст" in uninitialized
    assert "первую проверку" in uninitialized
    assert "В архиве пока нет квартир" in initialized
    assert "расширить бюджет" in initialized


def test_welcome_text_explains_first_action() -> None:
    text = _welcome_text()

    assert "Хорошие квартиры уходят за часы" in text
    assert "FlatPulse следит за новыми объявлениями" in text
    assert "узнавайте о новых квартирах сразу после публикации" in text


def test_initial_seed_text_does_not_claim_total_found_count() -> None:
    text = _initial_seed_text(28)

    assert "Поиск настроен" in text
    assert "Я нашел и запомнил текущие объявления: 28" in text
    assert "архиве" in text
    assert "найдено и запомнено" not in text


def test_first_entry_uses_start_reply_button() -> None:
    text = _first_entry_text()
    keyboard = _start_reply_keyboard().keyboard

    assert "Найдите квартиру раньше других" in text
    assert "Начнем настройку?" in text
    assert keyboard[0][0].text == "🔍 Настроить поиск"


def test_help_text_contains_support_contact() -> None:
    text = _help_text()

    assert "@FlatPulseSupport" in text


def test_manual_onboarding_parsers_accept_plain_text() -> None:
    assert _parse_manual_city("Москва") == ("Москва", "1")
    assert _parse_manual_city("Сочи") == ("Сочи", "4998")
    assert _parse_manual_city(" уфа ") == ("Уфа", "176245")
    assert _parse_manual_city("королев") == ("Королёв", "4813")
    assert _parse_manual_city("ростов на дону") == ("Ростов-на-Дону", "4959")
    assert _parse_manual_rooms("студия") == ("studio",)
    assert _parse_manual_rooms("1,2") == ("1", "2")
    assert _parse_manual_price("Любая") == (None, None)
    assert _parse_manual_price("до 60000") == (None, 60000)
    assert _parse_manual_price("60000-90000") == (60000, 90000)
    assert _parse_manual_rent("посуточная") == "short"


def test_manual_city_rejects_unknown_city_without_region_id_hint() -> None:
    try:
        _parse_manual_city("Город которого нет")
    except ConfigError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected ConfigError")

    assert "Не нашел такой город в ЦИАН" in message
    assert "region" not in message.lower()


def test_manual_check_cooldown_blocks_repeated_checks() -> None:
    last_check_at = "2026-05-31T10:00:00+00:00"

    assert (
        _manual_check_cooldown_remaining(
            None,
            now=datetime(2026, 5, 31, 10, 0, 0, tzinfo=UTC),
            cooldown_seconds=60,
        )
        == 0
    )
    assert (
        _manual_check_cooldown_remaining(
            last_check_at,
            now=datetime(2026, 5, 31, 10, 0, 20, tzinfo=UTC),
            cooldown_seconds=60,
        )
        == 40
    )
    assert (
        _manual_check_cooldown_remaining(
            last_check_at,
            now=datetime(2026, 5, 31, 10, 1, 1, tzinfo=UTC),
            cooldown_seconds=60,
        )
        == 0
    )
