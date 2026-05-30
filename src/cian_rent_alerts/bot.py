from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .cian_url import extract_polygon
from .config import ConfigError, Settings
from .db import ListingStore
from .geo import build_radius_polygon, geocode_address
from .service import build_search_url, run_check

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


CITY_OPTIONS = {
    "kazan": ("Казань", "4777"),
    "moscow": ("Москва", "1"),
    "spb": ("Санкт-Петербург", "2"),
    "ekb": ("Екатеринбург", "4743"),
    "nn": ("Нижний Новгород", "4885"),
}

PRICE_OPTIONS = {
    "35_45": ("35000", "45000", "35 000 - 45 000"),
    "45_60": ("45000", "60000", "45 000 - 60 000"),
    "to_45": ("none", "45000", "до 45 000"),
    "to_60": ("none", "60000", "до 60 000"),
    "none": ("none", "none", "без ограничения"),
}

ROOM_OPTIONS = {
    "studio": ("studio", "студия"),
    "1": ("1", "1"),
    "2": ("2", "2"),
    "12": ("1,2", "1-2"),
    "13": ("1,2,3", "1-3"),
    "all": ("all", "все"),
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
        if not settings.telegram_chat_id:
            raise ConfigError("TELEGRAM_CHAT_ID is required to run bot commands")
        self.settings = settings
        self.store = ListingStore(settings.database_path)
        self.store.init()

    def build_application(self) -> Application:
        application = Application.builder().token(self.settings.telegram_bot_token or "").build()
        application.add_handler(
            CommandHandler(["start", "help", "menu"], self._authorized(self.menu))
        )
        application.add_handler(CommandHandler("settings", self._authorized(self.settings_command)))
        application.add_handler(CommandHandler("setup", self._authorized(self.menu)))
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
        application.add_handler(CommandHandler("check", self._authorized(self.check)))
        application.add_handler(CallbackQueryHandler(self._authorized(self.on_callback)))
        return application

    def effective_settings(self) -> Settings:
        return self.settings.with_runtime_overrides(self.store.get_runtime_settings())

    def _authorized(self, handler: Handler) -> Handler:
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            expected_chat_id = self.settings.telegram_chat_id
            actual_chat_id = update.effective_chat.id if update.effective_chat else None
            if expected_chat_id and str(actual_chat_id) != str(expected_chat_id):
                logger.warning("Rejected Telegram command from chat_id=%s", actual_chat_id)
                return
            await handler(update, context)

        return wrapped

    async def menu(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _respond(
            update,
            "\n".join(
                [
                    "Меню настройки поиска.",
                    "",
                    "Через кнопки можно менять основные фильтры и запускать проверку.",
                ]
            ),
            _main_keyboard(),
        )

    async def settings_command(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _respond(update, _format_settings(self.effective_settings()), _main_keyboard())

    async def search_url(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await _reply(update, build_search_url(self.effective_settings()))
        except ConfigError as exc:
            await _reply(update, f"Ошибка настроек: {exc}")

    async def set_city(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args
        if not args:
            await _reply(update, "Использование: /set_city Казань 4777")
            return
        region_id = args[-1] if args[-1].isdigit() else None
        city_parts = args[:-1] if region_id else args
        city = " ".join(city_parts).strip()
        if not city:
            await _reply(update, "Укажите город: /set_city Казань 4777")
            return

        self.store.set_runtime_setting("cian_city", city)
        if region_id:
            self.store.set_runtime_setting("cian_region_id", region_id)
        self.store.set_runtime_setting("cian_use_generated_url", "true")
        await self._confirm_settings(update, "Город обновлен.")

    async def set_region(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1 or not context.args[0].isdigit():
            await _reply(update, "Использование: /set_region 4777")
            return
        self.store.set_runtime_setting("cian_region_id", context.args[0])
        self.store.set_runtime_setting("cian_use_generated_url", "true")
        await self._confirm_settings(update, "Region id обновлен.")

    async def set_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 2:
            await _reply(update, "Использование: /set_price 35000 45000")
            return
        min_price, max_price = context.args
        if not _is_optional_int(min_price) or not _is_optional_int(max_price):
            await _reply(update, "Цена должна быть числом или none: /set_price 35000 45000")
            return
        self._set_optional("cian_min_price", min_price)
        self._set_optional("cian_max_price", max_price)
        self.store.set_runtime_setting("cian_use_generated_url", "true")
        await self._confirm_settings(update, "Цена обновлена.")

    async def set_rooms(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1:
            await _reply(update, "Использование: /set_rooms 1,2")
            return
        rooms = context.args[0].strip().lower()
        if not _valid_rooms(rooms):
            await _reply(update, "Комнаты: 1..5, studio или all. Пример: /set_rooms 1,2")
            return
        self.store.set_runtime_setting("cian_rooms", rooms)
        self.store.set_runtime_setting("cian_use_generated_url", "true")
        await self._confirm_settings(update, "Комнаты обновлены.")

    async def set_rent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1 or context.args[0] not in {"long", "short"}:
            await _reply(update, "Использование: /set_rent long или /set_rent short")
            return
        self.store.set_runtime_setting("cian_rent_type", context.args[0])
        self.store.set_runtime_setting("cian_use_generated_url", "true")
        await self._confirm_settings(update, "Тип аренды обновлен.")

    async def set_sort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1:
            await _reply(update, "Использование: /set_sort creation_date_from_newer_to_older")
            return
        self.store.set_runtime_setting("cian_sort_by", context.args[0])
        self.store.set_runtime_setting("cian_use_generated_url", "true")
        await self._confirm_settings(update, "Сортировка обновлена.")

    async def set_area(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        value = " ".join(context.args).strip()
        if not value:
            await _reply(update, "Использование: /set_area https://kazan.cian.ru/map/?...")
            return
        if value.lower() in {"none", "clear", "off", "reset"}:
            self.store.delete_runtime_setting("cian_polygon")
            self.store.delete_runtime_setting("cian_area_label")
            await self._confirm_settings(update, "Область поиска очищена.")
            return
        try:
            polygon = extract_polygon(value)
        except ConfigError as exc:
            await _reply(update, f"Не удалось прочитать область: {exc}")
            return
        self.store.set_runtime_setting("cian_polygon", polygon)
        self.store.set_runtime_setting("cian_area_label", "выделенная область")
        self.store.set_runtime_setting("cian_use_generated_url", "true")
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

        self.store.set_runtime_setting("cian_polygon", polygon)
        self.store.set_runtime_setting("cian_area_label", f"{address}, {radius_meters} м")
        self.store.set_runtime_setting("cian_use_generated_url", "true")
        await self._confirm_settings(update, "Радиус поиска обновлен.")

    async def set_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        url = " ".join(context.args).strip()
        if not url.startswith(("http://", "https://")):
            await _reply(update, "Использование: /set_url https://cian.ru/cat.php?...")
            return
        self.store.set_runtime_setting("cian_search_url", url)
        self.store.set_runtime_setting("cian_use_generated_url", "false")
        await self._confirm_settings(update, "Ручная ссылка включена.")

    async def use_generated(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if len(context.args) != 1 or context.args[0].lower() not in {"true", "false"}:
            await _reply(update, "Использование: /use_generated true или /use_generated false")
            return
        self.store.set_runtime_setting("cian_use_generated_url", context.args[0].lower())
        await self._confirm_settings(update, "Режим URL обновлен.")

    async def reset_settings(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        self.store.clear_runtime_settings()
        await self._confirm_settings(update, "Настройки бота сброшены к .env.")

    async def mark_existing_sent(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        count = self.store.mark_all_unsent_as_sent()
        await _reply(update, f"Помечено как уже отправленное: {count}")

    async def check(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, "Запускаю проверку...")
        try:
            count = await asyncio.to_thread(run_check, self.settings)
        except RuntimeError as exc:
            await _reply(update, f"Ошибка проверки: {exc}")
            return
        await _reply(update, f"Проверка завершена. Новых уведомлений: {count}")

    async def on_callback(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()

        action = query.data
        if action == "cfg:menu":
            await _respond(update, "Меню настройки поиска.", _main_keyboard())
            return
        if action == "cfg:settings":
            await _respond(update, _format_settings(self.effective_settings()), _main_keyboard())
            return
        if action == "cfg:url":
            await self._show_search_url(update)
            return
        if action == "cfg:city":
            await _respond(update, "Выберите город:", _city_keyboard())
            return
        if action == "cfg:city_manual":
            await _respond(
                update,
                "Введите город командой:\n/set_city Казань 4777",
                _city_keyboard(),
            )
            return
        if action == "cfg:rooms":
            await _respond(update, "Выберите комнаты:", _rooms_keyboard())
            return
        if action == "cfg:rooms_manual":
            await _respond(
                update,
                "Введите комнаты командой:\n/set_rooms 1,2\n\nМожно указать 1..5, studio или all.",
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
            await _respond(
                update,
                "Введите цену командой:\n/set_price 35000 45000\n\nДля отсутствующей границы используйте none.",
                _price_keyboard(),
            )
            return
        if action == "cfg:rent":
            await _respond(update, "Выберите тип аренды:", _rent_keyboard())
            return
        if action == "cfg:rent_manual":
            await _respond(
                update,
                "Введите тип аренды командой:\n/set_rent long\n\nДоступно: long или short.",
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
            await _respond(
                update,
                "Введите адрес и радиус командой:\n"
                "/set_radius 1000 Казань, Кремлевская 18\n\n"
                "Также можно так:\n"
                "/set_radius Казань, Кремлевская 18 | 1000",
                _area_keyboard(),
            )
            return
        if action == "cfg:area_manual":
            await _respond(
                update,
                "1. Откройте карту ЦИАН.\n"
                "2. Выделите область.\n"
                "3. Скопируйте ссылку из браузера.\n"
                "4. Отправьте ее командой:\n"
                "/set_area https://kazan.cian.ru/map/?...",
                _area_keyboard(),
            )
            return
        if action == "cfg:area_clear":
            self.store.delete_runtime_setting("cian_polygon")
            self.store.delete_runtime_setting("cian_area_label")
            await self._confirm_settings(update, "Область поиска очищена.")
            return
        if action == "cfg:generated":
            self.store.set_runtime_setting("cian_use_generated_url", "true")
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
            await _respond(update, "Запускаю проверку...", _main_keyboard())
            try:
                count = await asyncio.to_thread(run_check, self.settings)
            except RuntimeError as exc:
                await _reply(update, f"Ошибка проверки: {exc}")
                return
            await _reply(update, f"Проверка завершена. Новых уведомлений: {count}")
            return
        if action == "cfg:mark":
            count = self.store.mark_all_unsent_as_sent()
            await _respond(update, f"Помечено как уже отправленное: {count}", _main_keyboard())
            return
        if action == "cfg:reset":
            self.store.clear_runtime_settings()
            await self._confirm_settings(update, "Настройки сброшены к .env.")
            return

        if action.startswith("cfg:city:"):
            key = action.rsplit(":", 1)[1]
            city, region_id = CITY_OPTIONS[key]
            self.store.set_runtime_setting("cian_city", city)
            self.store.set_runtime_setting("cian_region_id", region_id)
            self.store.set_runtime_setting("cian_use_generated_url", "true")
            await self._confirm_settings(update, "Город обновлен.")
            return
        if action.startswith("cfg:rooms:"):
            key = action.rsplit(":", 1)[1]
            rooms, _label = ROOM_OPTIONS[key]
            self.store.set_runtime_setting("cian_rooms", rooms)
            self.store.set_runtime_setting("cian_use_generated_url", "true")
            await self._confirm_settings(update, "Комнаты обновлены.")
            return
        if action.startswith("cfg:price:"):
            key = action.rsplit(":", 1)[1]
            min_price, max_price, _label = PRICE_OPTIONS[key]
            self._set_optional("cian_min_price", min_price)
            self._set_optional("cian_max_price", max_price)
            self.store.set_runtime_setting("cian_use_generated_url", "true")
            await self._confirm_settings(update, "Цена обновлена.")
            return
        if action.startswith("cfg:rent:"):
            rent_type = action.rsplit(":", 1)[1]
            self.store.set_runtime_setting("cian_rent_type", rent_type)
            self.store.set_runtime_setting("cian_use_generated_url", "true")
            await self._confirm_settings(update, "Тип аренды обновлен.")
            return
        if action.startswith("cfg:sort:"):
            key = action.rsplit(":", 1)[1]
            sort_value, _label = SORT_OPTIONS[key]
            self.store.set_runtime_setting("cian_sort_by", sort_value)
            self.store.set_runtime_setting("cian_use_generated_url", "true")
            await self._confirm_settings(update, "Сортировка обновлена.")

    async def _confirm_settings(self, update: Update, prefix: str) -> None:
        try:
            build_search_url(self.effective_settings())
        except ConfigError as exc:
            await _respond(
                update,
                f"{prefix}\nНо настройки сейчас некорректны: {exc}",
                _main_keyboard(),
            )
            return
        await _respond(
            update,
            f"{prefix}\n\n{_format_settings(self.effective_settings())}",
            _main_keyboard(),
        )

    async def _show_search_url(self, update: Update) -> None:
        try:
            url = build_search_url(self.effective_settings())
        except ConfigError as exc:
            await _respond(update, f"Ошибка настроек: {exc}", _main_keyboard())
            return
        await _respond(update, url, _main_keyboard())

    def _set_optional(self, key: str, value: str) -> None:
        if value.lower() == "none":
            self.store.delete_runtime_setting(key)
            return
        self.store.set_runtime_setting(key, value)


def build_settings_bot(settings: Settings) -> SettingsBot:
    return SettingsBot(settings)


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, disable_web_page_preview=True)


async def _respond(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
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
                InlineKeyboardButton("Настройки", callback_data="cfg:settings"),
                InlineKeyboardButton("URL поиска", callback_data="cfg:url"),
            ],
            [
                InlineKeyboardButton("Город", callback_data="cfg:city"),
                InlineKeyboardButton("Комнаты", callback_data="cfg:rooms"),
            ],
            [
                InlineKeyboardButton("Цена", callback_data="cfg:price"),
                InlineKeyboardButton("Тип аренды", callback_data="cfg:rent"),
            ],
            [
                InlineKeyboardButton("Область", callback_data="cfg:area"),
            ],
            [
                InlineKeyboardButton("Сортировка", callback_data="cfg:sort"),
                InlineKeyboardButton("Ручная ссылка", callback_data="cfg:manual_help"),
            ],
            [
                InlineKeyboardButton("Проверить сейчас", callback_data="cfg:check"),
            ],
            [
                InlineKeyboardButton("Только новые", callback_data="cfg:mark"),
                InlineKeyboardButton("Сбросить", callback_data="cfg:reset"),
            ],
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


def _format_settings(settings: Settings) -> str:
    source = "ручная ссылка" if not settings.cian_use_generated_url else "фильтры"
    return "\n".join(
        [
            "Текущий поиск",
            f"Город: {settings.cian_city}",
            f"Комнаты: {_format_rooms(settings.cian_rooms)}",
            f"Цена: {_format_price(settings.cian_min_price, settings.cian_max_price)}",
            f"Аренда: {_format_rent(settings.cian_rent_type)}",
            f"Область: {_format_area(settings.cian_polygon, settings.cian_area_label)}",
            f"Сортировка: {_format_sort(settings.cian_sort_by)}",
            f"Источник: {source}",
        ]
    )


def _format_rooms(rooms: tuple[str, ...]) -> str:
    if rooms == ("all",):
        return "все"
    return ", ".join("студия" if room == "studio" else room for room in rooms)


def _format_price(min_price: int | None, max_price: int | None) -> str:
    if min_price is None and max_price is None:
        return "без ограничения"
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
    return {"long": "долгосрочная", "short": "посуточная"}.get(value, value)


def _format_area(value: str | None, label: str | None) -> str:
    if not value:
        return "не задана"
    return label or "задана"


def _format_sort(value: str) -> str:
    labels = {sort: label for sort, label in SORT_OPTIONS.values()}
    return labels.get(value, value)


def _is_optional_int(value: str) -> bool:
    return value.lower() == "none" or value.isdigit()


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
