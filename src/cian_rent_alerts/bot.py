from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .cian_locations import find_cian_location
from .cian_url import extract_polygon
from .config import ConfigError, Settings
from .db import ListingStore
from .geo import build_radius_polygon, geocode_address
from .notifier import TelegramNotifier, send_listings_sync
from .service import CheckAlreadyRunning, build_search_url, run_check_result, settings_with_search

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
SHOW_FOUND_LIMIT = 20
MANUAL_CHECK_COOLDOWN_SECONDS = 60


CITY_OPTIONS = {
    "kazan": ("Казань", "4777"),
    "moscow": ("Москва", "1"),
    "spb": ("Санкт-Петербург", "2"),
    "ekb": ("Екатеринбург", "4743"),
    "nn": ("Нижний Новгород", "4885"),
}

PRICE_OPTIONS = {
    "none": ("none", "none", "Любой"),
    "35_45": ("35000", "45000", "35 000 - 45 000"),
    "45_60": ("45000", "60000", "45 000 - 60 000"),
    "60_90": ("60000", "90000", "60 000 - 90 000"),
    "90_130": ("90000", "130000", "90 000 - 130 000"),
    "to_45": ("none", "45000", "до 45 000"),
    "to_60": ("none", "60000", "до 60 000"),
}

ROOM_OPTIONS = {
    "studio": ("studio", "студия"),
    "1": ("1", "1"),
    "2": ("2", "2"),
    "12": ("1,2", "1-2"),
    "13": ("1,2,3", "1-3"),
    "all": ("all", "Любой"),
}

SORT_OPTIONS = {
    "new": ("creation_date_from_newer_to_older", "сначала новые"),
    "cheap": ("price_from_min_to_max", "сначала дешевые"),
    "expensive": ("price_from_max_to_min", "сначала дорогие"),
    "default": ("default", "по умолчанию"),
}


class SettingsBot:
    def __init__(self, settings: Settings) -> None:
        if not settings.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required to run bot commands")
        self.settings = settings
        self.store = ListingStore(settings.database_path, settings.database_url)
        self.store.init()

    def build_application(self) -> Application:
        application = Application.builder().token(self.settings.telegram_bot_token or "").build()
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler(["help", "menu"], self._authorized(self.menu)))
        application.add_handler(CommandHandler("settings", self._authorized(self.settings_command)))
        application.add_handler(CommandHandler("setup", self._authorized(self.setup_command)))
        application.add_handler(CommandHandler("search_url", self._authorized(self.search_url)))
        application.add_handler(CommandHandler("set_city", self._authorized(self.set_city)))
        application.add_handler(CommandHandler("set_region", self._authorized(self.set_region)))
        application.add_handler(CommandHandler("set_price", self._authorized(self.set_price)))
        application.add_handler(CommandHandler("set_rooms", self._authorized(self.set_rooms)))
        application.add_handler(CommandHandler("set_rent", self._authorized(self.set_rent)))
        application.add_handler(CommandHandler("set_sort", self._authorized(self.set_sort)))
        application.add_handler(CommandHandler("set_area", self._authorized(self.set_area)))
        application.add_handler(CommandHandler("set_radius", self._authorized(self.set_radius)))
        application.add_handler(CommandHandler("set_url", self._authorized(self.set_url)))
        application.add_handler(
            CommandHandler("use_generated", self._authorized(self.use_generated))
        )
        application.add_handler(
            CommandHandler("reset_settings", self._authorized(self.reset_settings))
        )
        application.add_handler(
            CommandHandler("mark_existing_sent", self._authorized(self.mark_existing_sent))
        )
        application.add_handler(CommandHandler("admin_status", self._admin_only(self.admin_status)))
        application.add_handler(CommandHandler("admin_users", self._admin_only(self.admin_users)))
        application.add_handler(
            CommandHandler("admin_searches", self._admin_only(self.admin_searches))
        )
        application.add_handler(
            CommandHandler("admin_last_runs", self._admin_only(self.admin_last_runs))
        )
        application.add_handler(CommandHandler("admin_errors", self._admin_only(self.admin_errors)))
        application.add_handler(CommandHandler("admin_health", self._admin_only(self.admin_health)))
        application.add_handler(CommandHandler("admin_report", self._admin_only(self.admin_report)))
        application.add_handler(CommandHandler("check", self._authorized(self.check)))
        application.add_handler(MessageHandler(filters.Regex("^Начать$"), self.begin_onboarding))
        application.add_handler(
            MessageHandler(filters.Regex("^Найти квартиру$"), self.find_apartment)
        )
        application.add_handler(MessageHandler(filters.Regex("^Меню$"), self.menu_from_reply))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        application.add_handler(CallbackQueryHandler(self._authorized(self.on_callback)))
        return application

    def effective_settings(self) -> Settings:
        return self.settings.with_runtime_overrides(self.store.get_runtime_settings())

    def effective_settings_for_update(self, update: Update) -> Settings:
        search = self._ensure_user_search(update)
        if search is None:
            return self.effective_settings()
        return settings_with_search(self.settings, search)

    def search_status_for_update(self, update: Update) -> str:
        search = self._ensure_user_search(update)
        if search is None:
            return "не настроен"
        return "активен" if search["is_active"] else "остановлен"

    def _authorized(self, handler: Handler) -> Handler:
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            actual_chat_id = update.effective_chat.id if update.effective_chat else None
            if actual_chat_id is None:
                return
            self._ensure_user_search(update)
            await handler(update, context)

        return wrapped

    def _admin_only(self, handler: Handler) -> Handler:
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            actual_chat_id = update.effective_chat.id if update.effective_chat else None
            if str(actual_chat_id) not in self.settings.admin_telegram_ids:
                logger.warning("Rejected admin Telegram command from chat_id=%s", actual_chat_id)
                return
            await handler(update, context)

        return wrapped

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return

        is_new_user = self.store.get_user_by_chat_id(str(chat.id)) is None
        self._ensure_user_search(update)
        if is_new_user:
            await _respond(update, _first_entry_text(), _start_reply_keyboard())
            return
        await _respond(update, _welcome_text(), _main_keyboard())

    async def begin_onboarding(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._ensure_user_search(update)
        self._clear_awaiting(update)
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "Начнем с города.",
                reply_markup=ReplyKeyboardRemove(),
                disable_web_page_preview=True,
            )
        await _respond(update, "Шаг 1 из 5. Выберите город:", _onboarding_city_keyboard())

    async def find_apartment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._ensure_user_search(update)
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "Открываю поиск.",
                reply_markup=ReplyKeyboardRemove(),
                disable_web_page_preview=True,
            )
        await _respond(update, _welcome_text(), _main_keyboard())

    async def menu_from_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._ensure_user_search(update)
        self._clear_awaiting(update)
        await _respond(update, _welcome_text(), _main_keyboard())

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        awaiting = self._get_awaiting(update)
        if not awaiting:
            await _respond(update, _help_text(), _main_keyboard())
            return

        value = update.effective_message.text.strip() if update.effective_message else ""
        if not value:
            return

        if awaiting == "city":
            await self._handle_manual_city(update, context, value)
            return
        if awaiting == "rooms":
            await self._handle_manual_rooms(update, context, value)
            return
        if awaiting == "price":
            await self._handle_manual_price(update, context, value)
            return
        if awaiting == "rent":
            await self._handle_manual_rent(update, context, value)
            return
        if awaiting == "radius":
            await self._handle_manual_radius(update, context, value)
            return
        if awaiting == "area":
            await self._handle_manual_area(update, context, value)
            return

        self._clear_awaiting(update)
        await _respond(update, _help_text(), _main_keyboard())

    async def _handle_manual_city(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
    ) -> None:
        try:
            city, region_id = _parse_manual_city(value)
        except ConfigError as exc:
            await _reply(update, str(exc))
            return
        self._update_current_search(update, city=city, region_id=region_id, use_generated_url=True)
        self._clear_awaiting(update)
        await _respond(update, "Шаг 2 из 5. Выберите комнаты:", _onboarding_rooms_keyboard())

    async def _handle_manual_rooms(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
    ) -> None:
        try:
            rooms = _parse_manual_rooms(value)
        except ConfigError as exc:
            await _reply(update, str(exc))
            return
        self._update_current_search(update, rooms=rooms, use_generated_url=True)
        self._clear_awaiting(update)
        await _respond(update, "Шаг 3 из 5. Выберите цену:", _onboarding_price_keyboard())

    async def _handle_manual_price(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
    ) -> None:
        try:
            min_price, max_price = _parse_manual_price(value)
        except ConfigError as exc:
            await _reply(update, str(exc))
            return
        self._update_current_search(
            update,
            min_price=min_price,
            max_price=max_price,
            use_generated_url=True,
        )
        self._clear_awaiting(update)
        await _respond(update, "Шаг 4 из 5. Выберите тип аренды:", _onboarding_rent_keyboard())

    async def _handle_manual_rent(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
    ) -> None:
        try:
            rent_type = _parse_manual_rent(value)
        except ConfigError as exc:
            await _reply(update, str(exc))
            return
        self._update_current_search(update, rent_type=rent_type, use_generated_url=True)
        self._clear_awaiting(update)
        await _respond(update, "Шаг 5 из 5. Выберите область:", _onboarding_area_keyboard())

    async def _handle_manual_radius(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
    ) -> None:
        try:
            radius_meters, address = _parse_radius_args(value.split())
            await _reply(update, f"Ищу адрес: {address}")
            coordinates = await asyncio.to_thread(
                geocode_address,
                address,
                user_agent="cian-rent-alerts",
                timeout_seconds=self.settings.request_timeout_seconds,
            )
            polygon = build_radius_polygon(
                latitude=coordinates.latitude,
                longitude=coordinates.longitude,
                radius_meters=radius_meters,
            )
        except ConfigError as exc:
            await _reply(update, f"Не удалось настроить радиус: {exc}")
            return

        self._update_current_search(
            update,
            polygon=polygon,
            area_label=_format_radius_label(address, radius_meters),
            use_generated_url=True,
        )
        self._clear_awaiting(update)
        await self._finish_onboarding(update)

    async def _handle_manual_area(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
    ) -> None:
        if value.strip().lower() in {"любая", "любой", "нет", "пропустить"}:
            self._update_current_search(
                update, polygon=None, area_label=None, use_generated_url=True
            )
            self._clear_awaiting(update)
            await self._finish_onboarding(update)
            return
        try:
            polygon = extract_polygon(value)
        except ConfigError as exc:
            await _reply(update, f"Не удалось прочитать область: {exc}")
            return
        self._update_current_search(
            update,
            polygon=polygon,
            area_label="выделенная область",
            use_generated_url=True,
        )
        self._clear_awaiting(update)
        await self._finish_onboarding(update)

    def _ensure_user_search(self, update: Update) -> dict[str, object] | None:
        chat = update.effective_chat
        if chat is None:
            return None

        user = update.effective_user
        chat_id = str(chat.id)
        user_id = self.store.upsert_user(
            telegram_chat_id=chat_id,
            telegram_user_id=str(user.id) if user is not None else None,
            username=user.username if user is not None else None,
            is_admin=chat_id in self.settings.admin_telegram_ids,
        )

        search = self.store.current_search_for_user(user_id)
        if search is not None:
            self.store.deactivate_other_searches_for_user(user_id, int(search["id"]))
            return search

        default_settings = self.effective_settings()
        self.store.create_search(
            user_id=user_id,
            title="Основной поиск",
            **_default_user_search_values(default_settings),
            is_active=False,
        )
        search = self.store.current_search_for_user(user_id)
        if search is not None:
            self.store.deactivate_other_searches_for_user(user_id, int(search["id"]))
        return search

    def _current_search(self, update: Update) -> dict[str, object] | None:
        chat = update.effective_chat
        if chat is None:
            return None
        user = self.store.get_user_by_chat_id(str(chat.id))
        if user is None:
            return None
        search = self.store.current_search_for_user(int(user["id"]))
        if search is not None:
            self.store.deactivate_other_searches_for_user(int(user["id"]), int(search["id"]))
        return search

    def _update_current_search(self, update: Update, **values: object) -> None:
        search = self._ensure_user_search(update)
        if search is None:
            raise ConfigError("Не удалось найти поиск пользователя")
        search_id = int(search["id"])
        self.store.update_search(search_id, **values, is_active=True, initialized_at=None)
        self.store.deactivate_other_searches_for_user(int(search["user_id"]), search_id)
        self.store.clear_seen_for_search(search_id)

    def _get_awaiting(self, update: Update) -> str | None:
        search = self._ensure_user_search(update)
        if search is None:
            return None
        return self.store.get_user_state(int(search["user_id"]), "awaiting")

    def _set_awaiting(self, update: Update, value: str) -> None:
        search = self._ensure_user_search(update)
        if search is None:
            return
        self.store.set_user_state(int(search["user_id"]), "awaiting", value)

    def _clear_awaiting(self, update: Update) -> None:
        search = self._ensure_user_search(update)
        if search is None:
            return
        self.store.delete_user_state(int(search["user_id"]), "awaiting")

    async def menu(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _respond(update, _welcome_text(), _main_keyboard())

    async def setup_command(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear_awaiting(update)
        await _respond(update, "Шаг 1 из 5. Выберите город:", _onboarding_city_keyboard())

    async def settings_command(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _respond(
            update,
            _format_settings(
                self.effective_settings_for_update(update),
                status=self.search_status_for_update(update),
            ),
            _main_keyboard(),
        )

    async def search_url(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await _reply(update, build_search_url(self.effective_settings_for_update(update)))
        except ConfigError as exc:
            await _reply(update, f"Ошибка настроек: {exc}")

    async def set_city(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args
        if not args:
            await _reply(update, "Использование: /set_city Сочи")
            return
        try:
            city, region_id = _parse_manual_city(" ".join(args))
        except ConfigError as exc:
            await _reply(update, str(exc))
            return

        self._update_current_search(
            update,
            city=city,
            region_id=region_id,
            use_generated_url=True,
        )
        await self._confirm_settings(update, "Город обновлен.")

    async def set_region(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1 or not context.args[0].isdigit():
            await _reply(update, "Использование: /set_region 4777")
            return
        self._update_current_search(update, region_id=context.args[0], use_generated_url=True)
        await self._confirm_settings(update, "Region id обновлен.")

    async def set_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 2:
            await _reply(update, "Использование: /set_price 35000 45000")
            return
        min_price, max_price = context.args
        if not _is_optional_int(min_price) or not _is_optional_int(max_price):
            await _reply(update, "Цена должна быть числом или none: /set_price 35000 45000")
            return
        self._update_current_search(
            update,
            min_price=_optional_int_from_command(min_price),
            max_price=_optional_int_from_command(max_price),
            use_generated_url=True,
        )
        await self._confirm_settings(update, "Цена обновлена.")

    async def set_rooms(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1:
            await _reply(update, "Использование: /set_rooms 1,2")
            return
        rooms = context.args[0].strip().lower()
        if not _valid_rooms(rooms):
            await _reply(update, "Комнаты: 1..5, studio или all. Пример: /set_rooms 1,2")
            return
        self._update_current_search(
            update, rooms=_rooms_from_command(rooms), use_generated_url=True
        )
        await self._confirm_settings(update, "Комнаты обновлены.")

    async def set_rent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1 or context.args[0] not in {"long", "short", "all"}:
            await _reply(update, "Использование: /set_rent long, /set_rent short или /set_rent all")
            return
        self._update_current_search(update, rent_type=context.args[0], use_generated_url=True)
        await self._confirm_settings(update, "Тип аренды обновлен.")

    async def set_sort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1:
            await _reply(update, "Использование: /set_sort creation_date_from_newer_to_older")
            return
        self._update_current_search(update, sort_by=context.args[0], use_generated_url=True)
        await self._confirm_settings(update, "Сортировка обновлена.")

    async def set_area(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        value = " ".join(context.args).strip()
        if not value:
            await _reply(update, "Использование: /set_area https://kazan.cian.ru/map/?...")
            return
        if value.lower() in {"none", "clear", "off", "reset"}:
            self._update_current_search(update, polygon=None, area_label=None)
            await self._confirm_settings(update, "Область поиска очищена.")
            return
        try:
            polygon = extract_polygon(value)
        except ConfigError as exc:
            await _reply(update, f"Не удалось прочитать область: {exc}")
            return
        self._update_current_search(
            update,
            polygon=polygon,
            area_label="выделенная область",
            use_generated_url=True,
        )
        await self._confirm_settings(update, "Область поиска обновлена.")

    async def set_radius(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            radius_meters, address = _parse_radius_args(context.args)
            await _reply(update, f"Ищу адрес: {address}")
            coordinates = await asyncio.to_thread(
                geocode_address,
                address,
                user_agent="cian-rent-alerts",
                timeout_seconds=self.settings.request_timeout_seconds,
            )
            polygon = build_radius_polygon(
                latitude=coordinates.latitude,
                longitude=coordinates.longitude,
                radius_meters=radius_meters,
            )
        except ConfigError as exc:
            await _reply(update, f"Не удалось настроить радиус: {exc}")
            return

        self._update_current_search(
            update,
            polygon=polygon,
            area_label=_format_radius_label(address, radius_meters),
            use_generated_url=True,
        )
        await self._confirm_settings(update, "Радиус поиска обновлен.")

    async def set_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        url = " ".join(context.args).strip()
        if not url.startswith(("http://", "https://")):
            await _reply(update, "Использование: /set_url https://cian.ru/cat.php?...")
            return
        self._update_current_search(update, manual_url=url, use_generated_url=False)
        await self._confirm_settings(update, "Ручная ссылка включена.")

    async def use_generated(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1 or context.args[0].lower() not in {"true", "false"}:
            await _reply(update, "Использование: /use_generated true или /use_generated false")
            return
        self._update_current_search(
            update,
            use_generated_url=context.args[0].lower() == "true",
        )
        await self._confirm_settings(update, "Режим URL обновлен.")

    async def reset_settings(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        self._update_current_search(update, **_default_user_search_values(self.settings))
        await self._confirm_settings(update, "Настройки поиска сброшены.")

    async def mark_existing_sent(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        search = self._ensure_user_search(update)
        if search is None:
            await _reply(update, "Не удалось найти поиск.")
            return
        count = self.store.mark_all_listings_seen_for_search(int(search["id"]))
        await _reply(update, f"Помечено как уже отправленное: {count}")

    async def check(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_current_check(update, _context)

    async def _run_current_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        search = self._ensure_user_search(update)
        if search is None:
            await _reply(update, "Не удалось найти поиск.")
            return
        if not search["is_active"]:
            await _respond(
                update,
                "Поиск остановлен. Новые объявления сейчас не отслеживаются.",
                _stopped_search_keyboard(),
            )
            return
        cooldown_until = _active_cooldown_until(search)
        if cooldown_until is not None:
            await _respond(
                update,
                "Этот поиск временно на паузе после ошибки парсинга.\n\n"
                f"Следующая попытка будет после {_format_timestamp(cooldown_until)}.",
                _ready_keyboard(),
            )
            return

        chat = update.effective_chat
        if chat is None:
            return

        user_id = int(search["user_id"])
        last_manual_check_at = self.store.get_user_state(user_id, "last_manual_check_at")
        cooldown_remaining = _manual_check_cooldown_remaining(last_manual_check_at)
        if cooldown_remaining > 0:
            await _reply(
                update,
                f"Проверка уже запускалась недавно. Попробуйте через {cooldown_remaining} сек.",
            )
            return

        was_unseeded = not search.get("initialized_at")
        self.store.set_user_state(
            user_id,
            "last_manual_check_at",
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        progress_message = await _reply(update, "Запускаю проверку...")
        try:
            result = await asyncio.to_thread(
                run_check_result,
                self.settings,
                only_chat_id=str(chat.id),
                only_search_id=int(search["id"]),
                fail_if_running=True,
            )
        except CheckAlreadyRunning:
            await _delete_message(progress_message)
            await _reply(update, "Проверка уже идет. Попробуйте через пару минут.")
            return
        except RuntimeError as exc:
            await _delete_message(progress_message)
            await _reply(update, f"Ошибка проверки: {exc}")
            return
        await _delete_message(progress_message)
        if was_unseeded:
            context.user_data["show_found_after_initial_seed"] = True
            current_run = self.store.get_check_run(result.run_id) if result.run_id else None
            found = current_run["listings_found"] if current_run else 0
            await _respond(
                update,
                _initial_seed_text(found),
                _show_found_keyboard(),
            )
            return
        await _reply(update, f"Проверка завершена. Новых уведомлений: {result.notifications_sent}")

    async def admin_status(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, _format_admin_status(self.store, self.effective_settings()))

    async def admin_health(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, _format_admin_health(self.store, self.effective_settings()))

    async def admin_users(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, _format_admin_users(self.store.recent_users()))

    async def admin_searches(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, _format_admin_searches(self.store.recent_searches()))

    async def admin_last_runs(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(
            update, _format_check_runs("Последние проверки", self.store.recent_check_runs())
        )

    async def admin_errors(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(
            update,
            _format_check_runs("Последние ошибки", self.store.recent_failed_check_runs()),
        )

    async def admin_report(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, _format_admin_report(self.store, self.effective_settings()))

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        action = query.data
        if action == "cfg:menu":
            await _respond(update, _welcome_text(), _main_keyboard())
            return
        if action == "cfg:help":
            await _respond(update, _help_text(), _main_keyboard())
            return
        if action == "cfg:setup":
            self._clear_awaiting(update)
            await _respond(update, "Шаг 1 из 5. Выберите город:", _onboarding_city_keyboard())
            return
        if action == "onb:step:rooms":
            await _respond(update, "Шаг 2 из 5. Выберите комнаты:", _onboarding_rooms_keyboard())
            return
        if action == "onb:step:price":
            await _respond(update, "Шаг 3 из 5. Выберите цену:", _onboarding_price_keyboard())
            return
        if action == "onb:step:rent":
            await _respond(update, "Шаг 4 из 5. Выберите тип аренды:", _onboarding_rent_keyboard())
            return
        if action == "onb:step:area":
            await _respond(update, "Шаг 5 из 5. Выберите область:", _onboarding_area_keyboard())
            return
        if action == "cfg:settings":
            await _respond(
                update,
                _format_settings(
                    self.effective_settings_for_update(update),
                    status=self.search_status_for_update(update),
                ),
                _main_keyboard(),
            )
            return
        if action == "cfg:url":
            await self._show_search_url(update)
            return
        if action == "cfg:city":
            await _respond(update, "Выберите город:", _city_keyboard())
            return
        if action == "cfg:city_manual":
            self._set_awaiting(update, "city")
            await _respond(
                update,
                "Введите город сообщением.\n\nНапример: Сочи, Уфа, Краснодар.",
                _city_keyboard(),
            )
            return
        if action == "cfg:rooms":
            await _respond(update, "Выберите комнаты:", _rooms_keyboard())
            return
        if action == "cfg:rooms_manual":
            self._set_awaiting(update, "rooms")
            await _respond(
                update,
                "Введите комнаты сообщением.\n\nМожно: Любой, студия, 1, 2, 1,2.",
                _rooms_keyboard(),
            )
            return
        if action == "cfg:price":
            await _respond(
                update,
                "Выберите диапазон цены:",
                _price_keyboard(),
            )
            return
        if action == "cfg:price_manual":
            self._set_awaiting(update, "price")
            await _respond(
                update,
                "Введите цену сообщением.\n\nМожно: Любая, до 60000, 60000-90000, от 90000.",
                _price_keyboard(),
            )
            return
        if action == "cfg:rent":
            await _respond(update, "Выберите тип аренды:", _rent_keyboard())
            return
        if action == "cfg:rent_manual":
            self._set_awaiting(update, "rent")
            await _respond(
                update,
                "Введите тип аренды сообщением.\n\nМожно: Любая, долгосрочная, посуточная.",
                _rent_keyboard(),
            )
            return
        if action == "cfg:sort":
            await _respond(update, "Выберите сортировку:", _sort_keyboard())
            return
        if action == "cfg:sort_manual":
            await _respond(
                update,
                "Введите сортировку командой:\n/set_sort creation_date_from_newer_to_older",
                _sort_keyboard(),
            )
            return
        if action == "cfg:area":
            await _respond(
                update,
                "Область поиска можно задать адресом с радиусом или ссылкой с карты ЦИАН.",
                _area_keyboard(),
            )
            return
        if action == "cfg:radius_manual":
            self._set_awaiting(update, "radius")
            await _respond(
                update,
                "Введите адрес и радиус сообщением.\n\n"
                "Например: 1000 Москва, Тверская 1\n"
                "Или: Москва, Тверская 1 | 1000",
                _area_keyboard(),
            )
            return
        if action == "cfg:area_manual":
            self._set_awaiting(update, "area")
            await _respond(
                update,
                "1. Откройте карту ЦИАН.\n"
                "2. Выделите область.\n"
                "3. Скопируйте ссылку из браузера.\n"
                "4. Отправьте ссылку следующим сообщением.",
                _area_keyboard(),
            )
            return
        if action == "cfg:area_clear":
            self._update_current_search(update, polygon=None, area_label=None)
            await self._confirm_settings(update, "Область поиска очищена.")
            return
        if action == "cfg:generated":
            self._update_current_search(update, use_generated_url=True)
            await self._confirm_settings(update, "Генерация URL включена.")
            return
        if action == "cfg:manual_help":
            await _respond(
                update,
                "Ручная ссылка заменит фильтры из меню.\n\nВведите ссылку командой:\n/set_url https://cian.ru/cat.php?...",
                _manual_url_keyboard(),
            )
            return
        if action == "cfg:check":
            await self._run_current_check(update, context)
            return
        if action == "cfg:show_found":
            await self._show_found_listings(update, context)
            return
        if action == "cfg:stop":
            await self._stop_current_search(update)
            return
        if action == "cfg:resume":
            await self._resume_current_search(update)
            return
        if action.startswith("onb:city:"):
            key = action.rsplit(":", 1)[1]
            city, region_id = CITY_OPTIONS[key]
            self._update_current_search(
                update,
                city=city,
                region_id=region_id,
                use_generated_url=True,
            )
            await _respond(update, "Шаг 2 из 5. Выберите комнаты:", _onboarding_rooms_keyboard())
            return
        if action == "onb:city_manual":
            self._set_awaiting(update, "city")
            await _respond(
                update,
                "Введите город сообщением.\n\nНапример: Сочи, Уфа, Краснодар.",
                _onboarding_city_keyboard(),
            )
            return
        if action.startswith("onb:rooms:"):
            key = action.rsplit(":", 1)[1]
            rooms, _label = ROOM_OPTIONS[key]
            self._update_current_search(
                update,
                rooms=_rooms_from_command(rooms),
                use_generated_url=True,
            )
            await _respond(update, "Шаг 3 из 5. Выберите цену:", _onboarding_price_keyboard())
            return
        if action == "onb:rooms_manual":
            self._set_awaiting(update, "rooms")
            await _respond(
                update,
                "Введите комнаты сообщением.\n\nМожно: Любой, студия, 1, 2, 1,2.",
                _onboarding_rooms_keyboard(),
            )
            return
        if action.startswith("onb:price:"):
            key = action.rsplit(":", 1)[1]
            min_price, max_price, _label = PRICE_OPTIONS[key]
            self._update_current_search(
                update,
                min_price=_optional_int_from_command(min_price),
                max_price=_optional_int_from_command(max_price),
                use_generated_url=True,
            )
            await _respond(update, "Шаг 4 из 5. Выберите тип аренды:", _onboarding_rent_keyboard())
            return
        if action == "onb:price_manual":
            self._set_awaiting(update, "price")
            await _respond(
                update,
                "Введите цену сообщением.\n\nМожно: Любая, до 60000, 60000-90000, от 90000.",
                _onboarding_price_keyboard(),
            )
            return
        if action.startswith("onb:rent:"):
            rent_type = action.rsplit(":", 1)[1]
            self._update_current_search(update, rent_type=rent_type, use_generated_url=True)
            await _respond(update, "Шаг 5 из 5. Выберите область:", _onboarding_area_keyboard())
            return
        if action == "onb:rent_manual":
            self._set_awaiting(update, "rent")
            await _respond(
                update,
                "Введите тип аренды сообщением.\n\nМожно: Любая, долгосрочная, посуточная.",
                _onboarding_rent_keyboard(),
            )
            return
        if action == "onb:area_skip":
            self._update_current_search(
                update, polygon=None, area_label=None, use_generated_url=True
            )
            await self._finish_onboarding(update)
            return
        if action == "onb:radius_manual":
            self._set_awaiting(update, "radius")
            await _respond(
                update,
                "Введите адрес и радиус сообщением.\n\n"
                "Например: 1000 Москва, Тверская 1\n"
                "Или: Москва, Тверская 1 | 1000",
                _onboarding_area_keyboard(),
            )
            return
        if action == "onb:area_manual":
            self._set_awaiting(update, "area")
            await _respond(
                update,
                "Отправьте ссылку с выделенной областью с карты ЦИАН.",
                _onboarding_area_keyboard(),
            )
            return
        if action == "cfg:mark":
            search = self._ensure_user_search(update)
            if search is None:
                await _respond(update, "Не удалось найти поиск.", _main_keyboard())
                return
            count = self.store.mark_all_listings_seen_for_search(int(search["id"]))
            await _respond(update, f"Помечено как уже отправленное: {count}", _main_keyboard())
            return
        if action == "cfg:reset":
            self._update_current_search(update, **_default_user_search_values(self.settings))
            await self._confirm_settings(update, "Настройки поиска сброшены.")
            return

        if action.startswith("cfg:city:"):
            key = action.rsplit(":", 1)[1]
            city, region_id = CITY_OPTIONS[key]
            self._update_current_search(
                update,
                city=city,
                region_id=region_id,
                use_generated_url=True,
            )
            await self._confirm_settings(update, "Город обновлен.")
            return
        if action.startswith("cfg:rooms:"):
            key = action.rsplit(":", 1)[1]
            rooms, _label = ROOM_OPTIONS[key]
            self._update_current_search(
                update,
                rooms=_rooms_from_command(rooms),
                use_generated_url=True,
            )
            await self._confirm_settings(update, "Комнаты обновлены.")
            return
        if action.startswith("cfg:price:"):
            key = action.rsplit(":", 1)[1]
            min_price, max_price, _label = PRICE_OPTIONS[key]
            self._update_current_search(
                update,
                min_price=_optional_int_from_command(min_price),
                max_price=_optional_int_from_command(max_price),
                use_generated_url=True,
            )
            await self._confirm_settings(update, "Цена обновлена.")
            return
        if action.startswith("cfg:rent:"):
            rent_type = action.rsplit(":", 1)[1]
            self._update_current_search(update, rent_type=rent_type, use_generated_url=True)
            await self._confirm_settings(update, "Тип аренды обновлен.")
            return
        if action.startswith("cfg:sort:"):
            key = action.rsplit(":", 1)[1]
            sort_value, _label = SORT_OPTIONS[key]
            self._update_current_search(update, sort_by=sort_value, use_generated_url=True)
            await self._confirm_settings(update, "Сортировка обновлена.")

    async def _finish_onboarding(self, update: Update) -> None:
        settings = self.effective_settings_for_update(update)
        await _respond(
            update,
            "Поиск настроен.\n\n"
            f"{_format_settings(settings, status=self.search_status_for_update(update))}\n\n"
            "Первый запуск запомнит текущие объявления. "
            "После этого новые варианты будут приходить автоматически.",
            _ready_keyboard(),
        )
        await _reply_with_markup(
            update,
            "Кнопка Меню теперь доступна снизу.",
            _configure_search_reply_keyboard(),
        )

    async def _confirm_settings(self, update: Update, prefix: str) -> None:
        try:
            effective_settings = self.effective_settings_for_update(update)
            build_search_url(effective_settings)
        except ConfigError as exc:
            await _respond(
                update,
                f"{prefix}\nНо настройки сейчас некорректны: {exc}",
                _main_keyboard(),
            )
            return
        await _respond(
            update,
            f"{prefix}\n\n{_format_settings(effective_settings, status=self.search_status_for_update(update))}",
            _main_keyboard(),
        )

    async def _show_search_url(self, update: Update) -> None:
        try:
            url = build_search_url(self.effective_settings_for_update(update))
        except ConfigError as exc:
            await _respond(update, f"Ошибка настроек: {exc}", _main_keyboard())
            return
        await _respond(update, url, _main_keyboard())

    async def _show_found_listings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        search = self._current_search(update)
        if search is None:
            await _reply(update, "Не удалось найти поиск.")
            return

        listings = self.store.listings_seen_for_search(
            int(search["id"]),
            limit=SHOW_FOUND_LIMIT,
        )
        if not listings:
            await _reply(update, "По текущим фильтрам пока ничего не найдено.")
            return

        if not self.settings.telegram_bot_token:
            await _reply(update, "TELEGRAM_BOT_TOKEN не задан.")
            return

        chat = update.effective_chat
        if chat is None:
            return

        await _reply(update, f"Показываю квартир: {len(listings)}")
        notifier = TelegramNotifier(
            token=self.settings.telegram_bot_token,
            chat_id=str(chat.id),
        )
        await asyncio.to_thread(send_listings_sync, notifier, listings)
        if context.user_data.pop("show_found_after_initial_seed", False):
            await _reply_with_markup(
                update,
                "Я сообщу, когда появятся новые квартиры.",
                _configure_search_reply_keyboard(),
            )

    async def _stop_current_search(self, update: Update) -> None:
        search = self._current_search(update)
        if search is None:
            await _reply(update, "Не удалось найти поиск.")
            return
        self.store.update_search(int(search["id"]), is_active=False)
        await _respond(
            update,
            "Поиск остановлен. Новые объявления по нему больше не будут приходить.",
            _stopped_search_keyboard(),
        )

    async def _resume_current_search(self, update: Update) -> None:
        search = self._current_search(update)
        if search is None:
            await _reply(update, "Не удалось найти поиск.")
            return
        search_id = int(search["id"])
        self.store.update_search(search_id, is_active=True, initialized_at=None)
        self.store.clear_seen_for_search(search_id)
        await _respond(
            update,
            "Поиск возобновлен. Первый запуск запомнит текущие объявления, "
            "а дальше будут приходить только новые.",
            _ready_keyboard(),
        )


def build_settings_bot(settings: Settings) -> SettingsBot:
    return SettingsBot(settings)


def _search_values_from_settings(settings: Settings) -> dict[str, object]:
    return {
        "city": settings.cian_city,
        "region_id": settings.cian_region_id,
        "rooms": settings.cian_rooms,
        "min_price": settings.cian_min_price,
        "max_price": settings.cian_max_price,
        "rent_type": settings.cian_rent_type,
        "sort_by": settings.cian_sort_by,
        "polygon": settings.cian_polygon,
        "area_label": settings.cian_area_label,
        "manual_url": settings.cian_search_url,
        "use_generated_url": settings.cian_use_generated_url,
    }


def _default_user_search_values(settings: Settings) -> dict[str, object]:
    return {
        "city": "Москва",
        "region_id": "1",
        "rooms": ("all",),
        "min_price": None,
        "max_price": None,
        "rent_type": "all",
        "sort_by": settings.cian_sort_by,
        "polygon": None,
        "area_label": None,
        "manual_url": None,
        "use_generated_url": True,
    }


async def _reply(update: Update, text: str) -> Message | None:
    if update.effective_message is not None:
        return await update.effective_message.reply_text(text, disable_web_page_preview=True)
    return None


async def _delete_message(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError:
        logger.debug("Failed to delete Telegram progress message", exc_info=True)


async def _reply_with_markup(
    update: Update,
    text: str,
    reply_markup: ReplyKeyboardMarkup | ReplyKeyboardRemove,
) -> None:
    target_message = update.effective_message
    if update.callback_query is not None and update.callback_query.message is not None:
        target_message = update.callback_query.message
    if target_message is not None:
        await target_message.reply_text(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


async def _respond(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
) -> None:
    if update.callback_query is not None and update.callback_query.message is not None:
        try:
            await update.callback_query.message.edit_text(
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except TelegramError:
            logger.debug("Failed to edit Telegram message, sending a new one", exc_info=True)
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍 Настроить поиск", callback_data="cfg:setup"),
            ],
            [
                InlineKeyboardButton("⚡ Проверить новые квартиры", callback_data="cfg:check"),
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data="cfg:settings"),
                InlineKeyboardButton("❓ Как это работает", callback_data="cfg:help"),
            ],
            [
                InlineKeyboardButton("⏸️ Остановить уведомления", callback_data="cfg:stop"),
            ],
        ]
    )


def _start_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["Начать"]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите, чтобы настроить поиск",
    )


def _configure_search_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["Меню"]],
        resize_keyboard=True,
        input_field_placeholder="Открыть меню",
    )


def _setup_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Настроить поиск", callback_data="cfg:setup")],
            [InlineKeyboardButton("Мои настройки", callback_data="cfg:settings")],
            [InlineKeyboardButton("Помощь", callback_data="cfg:help")],
        ]
    )


def _stopped_search_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Возобновить поиск", callback_data="cfg:resume")],
            [InlineKeyboardButton("Настроить заново", callback_data="cfg:setup")],
            [InlineKeyboardButton("Мои настройки", callback_data="cfg:settings")],
        ]
    )


def _ready_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Проверить сейчас", callback_data="cfg:check")],
            [
                InlineKeyboardButton("Мои настройки", callback_data="cfg:settings"),
                InlineKeyboardButton("Настроить заново", callback_data="cfg:setup"),
            ],
        ]
    )


def _onboarding_city_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Москва", callback_data="onb:city:moscow"),
                InlineKeyboardButton("Казань", callback_data="onb:city:kazan"),
            ],
            [
                InlineKeyboardButton("Санкт-Петербург", callback_data="onb:city:spb"),
                InlineKeyboardButton("Ввести вручную", callback_data="onb:city_manual"),
            ],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _onboarding_rooms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Любой", callback_data="onb:rooms:all"),
                InlineKeyboardButton("Студия", callback_data="onb:rooms:studio"),
                InlineKeyboardButton("1", callback_data="onb:rooms:1"),
            ],
            [
                InlineKeyboardButton("2", callback_data="onb:rooms:2"),
                InlineKeyboardButton("1-2", callback_data="onb:rooms:12"),
                InlineKeyboardButton("1-3", callback_data="onb:rooms:13"),
            ],
            [InlineKeyboardButton("Ввести вручную", callback_data="onb:rooms_manual")],
            [InlineKeyboardButton("Назад", callback_data="cfg:setup")],
        ]
    )


def _onboarding_price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Любая", callback_data="onb:price:none"),
                InlineKeyboardButton("до 60 000", callback_data="onb:price:to_60"),
            ],
            [
                InlineKeyboardButton("60 000 - 90 000", callback_data="onb:price:60_90"),
                InlineKeyboardButton("90 000 - 130 000", callback_data="onb:price:90_130"),
            ],
            [InlineKeyboardButton("Ввести вручную", callback_data="onb:price_manual")],
            [InlineKeyboardButton("Назад", callback_data="onb:step:rooms")],
        ]
    )


def _onboarding_rent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Любая", callback_data="onb:rent:all"),
                InlineKeyboardButton("Долгосрочная", callback_data="onb:rent:long"),
            ],
            [InlineKeyboardButton("Посуточная", callback_data="onb:rent:short")],
            [InlineKeyboardButton("Ввести вручную", callback_data="onb:rent_manual")],
            [InlineKeyboardButton("Назад", callback_data="onb:step:price")],
        ]
    )


def _onboarding_area_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Любая область", callback_data="onb:area_skip")],
            [InlineKeyboardButton("Адрес и радиус", callback_data="onb:radius_manual")],
            [InlineKeyboardButton("Ссылка с карты", callback_data="onb:area_manual")],
            [InlineKeyboardButton("Назад", callback_data="onb:step:rent")],
        ]
    )


def _show_found_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Показать квартиры", callback_data="cfg:show_found")],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _city_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(city, callback_data=f"cfg:city:{key}")]
        for key, (city, _region_id) in CITY_OPTIONS.items()
    ]
    rows.append([InlineKeyboardButton("Ввести вручную", callback_data="cfg:city_manual")])
    rows.append([InlineKeyboardButton("Назад", callback_data="cfg:menu")])
    return InlineKeyboardMarkup(rows)


def _rooms_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"cfg:rooms:{key}")
                for key, (_rooms, label) in list(ROOM_OPTIONS.items())[:3]
            ],
            [
                InlineKeyboardButton(label, callback_data=f"cfg:rooms:{key}")
                for key, (_rooms, label) in list(ROOM_OPTIONS.items())[3:]
            ],
            [InlineKeyboardButton("Ввести вручную", callback_data="cfg:rooms_manual")],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"cfg:price:{key}")
                for key, (_min_price, _max_price, label) in list(PRICE_OPTIONS.items())[:2]
            ],
            [
                InlineKeyboardButton(label, callback_data=f"cfg:price:{key}")
                for key, (_min_price, _max_price, label) in list(PRICE_OPTIONS.items())[2:4]
            ],
            [
                InlineKeyboardButton(
                    PRICE_OPTIONS["none"][2],
                    callback_data="cfg:price:none",
                )
            ],
            [InlineKeyboardButton("Ввести вручную", callback_data="cfg:price_manual")],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _rent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Любой", callback_data="cfg:rent:all"),
                InlineKeyboardButton("Долгосрочная", callback_data="cfg:rent:long"),
                InlineKeyboardButton("Посуточная", callback_data="cfg:rent:short"),
            ],
            [InlineKeyboardButton("Ввести вручную", callback_data="cfg:rent_manual")],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _sort_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"cfg:sort:{key}")
                for key, (_sort, label) in list(SORT_OPTIONS.items())[:2]
            ],
            [
                InlineKeyboardButton(label, callback_data=f"cfg:sort:{key}")
                for key, (_sort, label) in list(SORT_OPTIONS.items())[2:]
            ],
            [InlineKeyboardButton("Ввести вручную", callback_data="cfg:sort_manual")],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _area_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Адрес и радиус", callback_data="cfg:radius_manual")],
            [InlineKeyboardButton("Ссылка с карты", callback_data="cfg:area_manual")],
            [InlineKeyboardButton("Очистить область", callback_data="cfg:area_clear")],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cfg:menu")]])


def _manual_url_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Ввести вручную", callback_data="cfg:manual_help")],
            [InlineKeyboardButton("Использовать фильтры", callback_data="cfg:generated")],
            [InlineKeyboardButton("Назад", callback_data="cfg:menu")],
        ]
    )


def _first_entry_text() -> str:
    return "\n".join(
        [
            "Я помогу найти квартиру и буду следить за новыми объявлениями.",
            "",
            "Настрой город, комнаты, цену, тип аренды и область поиска. "
            "Я сохраню текущую выдачу, не буду присылать дубли и сообщу, "
            "когда появятся новые подходящие квартиры.",
            "",
            "Сейчас поиск работает по ЦИАН. Новые объявления проверяются автоматически.",
            "",
            "Нажмите Начать, чтобы настроить поиск.",
        ]
    )


def _welcome_text() -> str:
    return "\n".join(
        [
            "🏠 Не проверяйте ЦИАН каждые 10 минут.",
            "",
            "FlatPulse сам отслеживает новые квартиры и присылает только свежие объявления "
            "по вашим параметрам.",
            "",
            "Настройте поиск один раз — и получайте уведомления, как только появляется "
            "подходящий вариант.",
        ]
    )


def _initial_seed_text(found: object) -> str:
    return (
        f"Первый запуск: запомнил первые объявления из текущей выдачи: {found}. "
        "Дальше будут приходить только новые."
    )


def _help_text() -> str:
    return "\n".join(
        [
            "Как пользоваться:",
            "1. Нажмите Настроить поиск.",
            "2. Выберите город, комнаты, цену, тип аренды и область.",
            "3. Нажмите Проверить сейчас.",
            "",
            "Если хотите ввести точные значения вручную, используйте подсказки в разделах настройки.",
        ]
    )


def _format_settings(settings: Settings, *, status: str | None = None) -> str:
    source = "ручная ссылка" if not settings.cian_use_generated_url else "фильтры"
    lines = ["Текущий поиск"]
    if status is not None:
        lines.append(f"Статус: {status}")
    lines.extend(
        [
            f"Город: {settings.cian_city}",
            f"Комнаты: {_format_rooms(settings.cian_rooms)}",
            f"Цена: {_format_price(settings.cian_min_price, settings.cian_max_price)}",
            f"Аренда: {_format_rent(settings.cian_rent_type)}",
            f"Область: {_format_area(settings.cian_polygon, settings.cian_area_label)}",
            f"Сортировка: {_format_sort(settings.cian_sort_by)}",
            f"Источник: {source}",
        ]
    )
    return "\n".join(lines)


def _format_admin_status(store: ListingStore, settings: Settings) -> str:
    last_run = store.last_check_run()
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
    daily_summary = store.check_runs_summary_since(since)
    total_searches = store.searches_count_by_status()
    active_searches = store.searches_count_by_status(active=True)
    stopped_searches = store.searches_count_by_status(active=False)
    cooldown_searches = store.cooldown_searches_count()
    lines = [
        "Admin status",
        f"Interval: {_format_interval(settings.check_interval_seconds)}",
        f"Dry run: {'yes' if settings.dry_run else 'no'}",
        f"Playwright: {'yes' if settings.use_playwright else 'no'}",
        f"Users: {store.users_count()} total, {store.users_count(active=True)} active",
        f"Searches: {total_searches} total, {active_searches} active, "
        f"{stopped_searches} stopped, {cooldown_searches} cooldown",
        f"Last 24h: runs={daily_summary['runs']} partial={daily_summary['partial_runs']} "
        f"failed={daily_summary['failed_runs']} sent={daily_summary['notifications_sent']}",
    ]
    if last_run is None:
        lines.append("Last check: none")
        return "\n".join(lines)

    lines.extend(
        [
            f"Last check: {_format_timestamp(last_run.get('finished_at') or last_run['started_at'])}",
            f"Status: {last_run['status']}",
            f"Found: {last_run['listings_found']}",
            f"New: {last_run['new_listings']}",
            f"Sent: {last_run['notifications_sent']}",
        ]
    )
    if last_run.get("error"):
        lines.append(f"Error: {last_run['error']}")
    return "\n".join(lines)


def _format_admin_health(store: ListingStore, settings: Settings) -> str:
    try:
        store.ping()
        schema_version = store.schema_version() or "unknown"
        last_run = store.last_check_run()
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        daily_summary = store.check_runs_summary_since(since)
        users_total = store.users_count()
        users_active = store.users_count(active=True)
        searches_total = store.searches_count_by_status()
        searches_active = store.searches_count_by_status(active=True)
        cooldown_searches = store.cooldown_searches_count()
    except Exception as exc:
        return "\n".join(["Admin health", "DB: error", f"Error: {str(exc)[:300]}"])

    problem_runs = daily_summary["failed_runs"] + daily_summary["partial_runs"]
    health = "ok" if problem_runs == 0 else "degraded"
    lines = [
        "Admin health",
        f"Health: {health}",
        "DB: ok",
        f"Schema: {schema_version}",
        f"Interval: {_format_interval(settings.check_interval_seconds)}",
        f"Users: {users_total} total, {users_active} active",
        f"Searches: {searches_total} total, {searches_active} active, {cooldown_searches} cooldown",
        f"Parser: {_format_parser_mode(settings)}, retry={settings.parser_retry_attempts} "
        f"backoff={_format_interval(settings.parser_retry_backoff_seconds)}",
        f"Limits: search_delay={_format_interval(settings.search_check_delay_seconds)} "
        f"problem_cooldown={_format_interval(settings.parser_problem_cooldown_seconds)} "
        f"network_cooldown={_format_interval(settings.parser_network_cooldown_seconds)}",
        f"Last 24h: runs={daily_summary['runs']} partial={daily_summary['partial_runs']} "
        f"failed={daily_summary['failed_runs']} sent={daily_summary['notifications_sent']}",
    ]
    if last_run is None:
        lines.append("Last check: none")
        return "\n".join(lines)

    lines.extend(
        [
            f"Last check: {_format_timestamp(last_run.get('finished_at') or last_run['started_at'])}",
            f"Last status: {last_run['status']}",
            f"Last found: {last_run['listings_found']}",
            f"Last sent: {last_run['notifications_sent']}",
        ]
    )
    return "\n".join(lines)


def _format_admin_users(users: list[dict[str, object]]) -> str:
    if not users:
        return "Admin users\nНет пользователей"

    lines = ["Admin users"]
    for user in users:
        username = user.get("username") or "-"
        active = "active" if user.get("is_active") else "inactive"
        admin = " admin" if user.get("is_admin") else ""
        lines.append(
            f"#{user['id']} chat={user['telegram_chat_id']} @{username} "
            f"{active}{admin} last={_format_timestamp(user.get('last_seen_at'))}"
        )
    return "\n".join(lines)


def _format_admin_searches(searches: list[dict[str, object]]) -> str:
    if not searches:
        return "Admin searches\nНет поисков"

    lines = ["Admin searches"]
    for search in searches:
        username = search.get("username") or "-"
        status = "active" if search.get("is_active") else "stopped"
        cooldown_until = _active_cooldown_until(search)
        if cooldown_until is not None:
            status = f"cooldown until {_format_timestamp(cooldown_until)}"
        lines.append(
            f"#{search['id']} chat={search['telegram_chat_id']} @{username} {status} "
            f"{search['city']} rooms={_format_rooms(_rooms_from_db(search.get('rooms')))} "
            f"price={_format_price(_optional_int_from_db(search.get('min_price')), _optional_int_from_db(search.get('max_price')))} "
            f"rent={_format_rent(str(search['rent_type']))}"
        )
    return "\n".join(lines)


def _format_admin_report(store: ListingStore, settings: Settings) -> str:
    return "\n\n".join(
        [
            _format_admin_health(store, settings),
            _format_admin_status(store, settings),
            _format_admin_searches(store.recent_searches(limit=3)),
            _format_check_runs("Последние проверки", store.recent_check_runs(limit=3)),
            _format_check_runs("Последние ошибки", store.recent_failed_check_runs(limit=3)),
        ]
    )


def _active_cooldown_until(search: dict[str, object]) -> str | None:
    raw_value = search.get("cooldown_until")
    if not raw_value:
        return None
    value = str(raw_value)
    try:
        cooldown_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if cooldown_at.tzinfo is None:
        cooldown_at = cooldown_at.replace(tzinfo=UTC)
    if cooldown_at <= datetime.now(UTC):
        return None
    return value


def _format_check_runs(title: str, runs: list[dict[str, object]]) -> str:
    if not runs:
        return f"{title}\nНет данных"

    lines = [title]
    for run in runs:
        line = (
            f"#{run['id']} {_format_timestamp(run.get('finished_at') or run['started_at'])} "
            f"{run['status']} found={run['listings_found']} "
            f"new={run['new_listings']} sent={run['notifications_sent']}"
        )
        lines.append(line)
        if run.get("error"):
            lines.append(f"  error: {str(run['error'])[:240]}")
    return "\n".join(lines)


def _format_interval(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    if minutes and not rest:
        return f"{minutes} min"
    return f"{seconds} sec"


def _format_parser_mode(settings: Settings) -> str:
    if settings.use_playwright:
        return "playwright"
    if settings.playwright_fallback:
        return "requests+playwright_fallback"
    return "requests"


def _manual_check_cooldown_remaining(
    last_check_at: str | None,
    *,
    now: datetime | None = None,
    cooldown_seconds: int = MANUAL_CHECK_COOLDOWN_SECONDS,
) -> int:
    if not last_check_at:
        return 0

    now = now or datetime.now(UTC)
    try:
        last_check = datetime.fromisoformat(last_check_at)
    except ValueError:
        return 0
    if last_check.tzinfo is None:
        last_check = last_check.replace(tzinfo=UTC)

    remaining = cooldown_seconds - (now - last_check).total_seconds()
    if remaining <= 0:
        return 0
    return math.ceil(remaining)


def _format_timestamp(value: object) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_rooms(rooms: tuple[str, ...]) -> str:
    if rooms == ("all",):
        return "Любой"
    return ", ".join("студия" if room == "studio" else room for room in rooms)


def _rooms_from_db(value: object) -> tuple[str, ...]:
    if value is None or str(value).strip() == "":
        return ("all",)
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _optional_int_from_db(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _format_price(min_price: int | None, max_price: int | None) -> str:
    if min_price is None and max_price is None:
        return "Любой"
    if min_price is None:
        return f"до {_format_money(max_price)}"
    if max_price is None:
        return f"от {_format_money(min_price)}"
    return f"{_format_money(min_price)} - {_format_money(max_price)}"


def _format_money(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value:,} ₽".replace(",", " ")


def _format_rent(value: str) -> str:
    return {"long": "долгосрочная", "short": "посуточная", "all": "Любой"}.get(value, value)


def _format_area(value: str | None, label: str | None) -> str:
    if not value:
        return "Любая"
    if not label:
        return "область задана"
    return _normalize_area_label(label)


def _format_radius_label(address: str, radius_meters: int) -> str:
    return f"радиус {radius_meters} м от: {address}"


def _normalize_area_label(label: str) -> str:
    match = re.fullmatch(r"(.+),\s*(\d+)\s*м", label.strip())
    if match is None:
        return label
    address, radius_meters = match.groups()
    return _format_radius_label(address, int(radius_meters))


def _format_sort(value: str) -> str:
    labels = {sort: label for sort, label in SORT_OPTIONS.values()}
    return labels.get(value, value)


def _is_optional_int(value: str) -> bool:
    return value.lower() == "none" or value.isdigit()


def _optional_int_from_command(value: str) -> int | None:
    if value.lower() == "none":
        return None
    return int(value)


def _rooms_from_command(value: str) -> tuple[str, ...]:
    return tuple(room.strip() for room in value.split(",") if room.strip())


def _parse_manual_city(value: str) -> tuple[str, str]:
    location = find_cian_location(value)
    if location is None:
        raise ConfigError(
            "Не нашел такой город в ЦИАН. Проверьте написание или попробуйте другой город."
        )
    return location


def _parse_manual_rooms(value: str) -> tuple[str, ...]:
    normalized = value.strip().lower().replace(" ", "")
    if normalized in {"любой", "любая", "все", "all"}:
        return ("all",)
    normalized = normalized.replace("студия", "studio")
    if not _valid_rooms(normalized):
        raise ConfigError("Комнаты: Любой, студия, 1, 2 или 1,2.")
    return _rooms_from_command(normalized)


def _parse_manual_price(value: str) -> tuple[int | None, int | None]:
    raw = value.strip().lower()
    parts = raw.split()
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        return int(parts[0]), int(parts[1])

    normalized = raw.replace(" ", "")
    if normalized in {"любой", "любая", "нет", "all", "none"}:
        return None, None

    if normalized.startswith("до"):
        max_price = normalized.removeprefix("до")
        if max_price.isdigit():
            return None, int(max_price)
    if normalized.startswith("от"):
        min_price = normalized.removeprefix("от")
        if min_price.isdigit():
            return int(min_price), None

    match = re.fullmatch(r"(\d+)[-–—,](\d+)", normalized)
    if match:
        return int(match.group(1)), int(match.group(2))

    raise ConfigError("Цена: Любая, до 60000, 60000-90000 или от 90000.")


def _parse_manual_rent(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"любой", "любая", "all", "все"}:
        return "all"
    if normalized in {"долгосрочная", "долгосрочно", "long"}:
        return "long"
    if normalized in {"посуточная", "посуточно", "short"}:
        return "short"
    raise ConfigError("Тип аренды: Любая, долгосрочная или посуточная.")


def _valid_rooms(value: str) -> bool:
    if value == "all":
        return True
    for room in value.split(","):
        room = room.strip()
        if room == "studio":
            continue
        if not room.isdigit() or not 1 <= int(room) <= 5:
            return False
    return True


def _parse_radius_args(args: list[str]) -> tuple[int, str]:
    raw = " ".join(args).strip()
    if not raw:
        raise ConfigError("Use /set_radius 1000 Казань, Кремлевская 18")

    if "|" in raw:
        address, radius = [part.strip() for part in raw.rsplit("|", 1)]
    else:
        parts = raw.split(maxsplit=1)
        if len(parts) != 2:
            raise ConfigError("Use /set_radius 1000 Казань, Кремлевская 18")
        radius, address = parts

    if not radius.isdigit():
        raise ConfigError("Radius must be a number in meters")
    if not address:
        raise ConfigError("Address is empty")

    return int(radius), address
