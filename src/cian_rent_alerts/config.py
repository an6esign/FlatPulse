from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _as_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _parse_optional_int(name: str, value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _as_rooms(value: str | None) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return ("1", "2")
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _as_csv_set(value: str | None) -> frozenset[str]:
    if value is None or value.strip() == "":
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _as_optional_path(value: str | None) -> Path | None:
    if value is None or value.strip() == "":
        return None
    return Path(value)


@dataclass(frozen=True, slots=True)
class Settings:
    cian_search_url: str | None
    cian_use_generated_url: bool
    cian_city: str
    cian_region_id: str | None
    cian_rooms: tuple[str, ...]
    cian_min_price: int | None
    cian_max_price: int | None
    cian_rent_type: str
    cian_sort_by: str
    cian_polygon: str | None
    cian_area_label: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    admin_telegram_ids: frozenset[str]
    database_url: str | None
    database_path: Path
    parser_debug_dir: Path | None
    check_interval_seconds: int
    search_check_delay_seconds: int
    listing_limit: int
    listing_max_age_days: int
    dry_run: bool
    request_timeout_seconds: int
    parser_retry_attempts: int
    parser_retry_backoff_seconds: int
    parser_problem_cooldown_seconds: int
    parser_network_cooldown_seconds: int
    user_agent: str
    use_playwright: bool
    playwright_fallback: bool
    playwright_headless: bool
    environment: str
    prod_telegram_bot_token_hash: str | None
    yookassa_shop_id: str | None
    yookassa_secret_key: str | None
    yookassa_return_url: str | None
    subscription_price_rub: int
    subscription_period_days: int
    trial_days: int

    @classmethod
    def from_env(cls, env_file: Path | None = Path(".env")) -> "Settings":
        if env_file is not None:
            _load_dotenv(env_file)

        cian_search_url = os.getenv("CIAN_SEARCH_URL", "").strip()

        return cls(
            cian_search_url=cian_search_url or None,
            cian_use_generated_url=_as_bool(os.getenv("CIAN_USE_GENERATED_URL"), True),
            cian_city=os.getenv("CIAN_CITY", "Казань").strip(),
            cian_region_id=os.getenv("CIAN_REGION_ID") or None,
            cian_rooms=_as_rooms(os.getenv("CIAN_ROOMS")),
            cian_min_price=_as_optional_int("CIAN_MIN_PRICE"),
            cian_max_price=_as_optional_int("CIAN_MAX_PRICE"),
            cian_rent_type=os.getenv("CIAN_RENT_TYPE", "long").strip().lower(),
            cian_sort_by=os.getenv("CIAN_SORT_BY", "creation_date_from_newer_to_older").strip(),
            cian_polygon=os.getenv("CIAN_POLYGON") or None,
            cian_area_label=os.getenv("CIAN_AREA_LABEL") or None,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            admin_telegram_ids=_as_csv_set(os.getenv("ADMIN_TELEGRAM_IDS")),
            database_url=os.getenv("DATABASE_URL") or None,
            database_path=Path(os.getenv("DATABASE_PATH", "data/listings.sqlite3")),
            parser_debug_dir=_as_optional_path(os.getenv("PARSER_DEBUG_DIR")),
            check_interval_seconds=max(_as_int("CHECK_INTERVAL_SECONDS", 600), 60),
            search_check_delay_seconds=max(_as_int("SEARCH_CHECK_DELAY_SECONDS", 5), 0),
            listing_limit=max(_as_int("LISTING_LIMIT", 50), 1),
            listing_max_age_days=max(_as_int("LISTING_MAX_AGE_DAYS", 2), 0),
            dry_run=_as_bool(os.getenv("DRY_RUN"), False),
            request_timeout_seconds=max(_as_int("REQUEST_TIMEOUT_SECONDS", 20), 1),
            parser_retry_attempts=max(_as_int("PARSER_RETRY_ATTEMPTS", 2), 1),
            parser_retry_backoff_seconds=max(_as_int("PARSER_RETRY_BACKOFF_SECONDS", 2), 0),
            parser_problem_cooldown_seconds=max(
                _as_int("PARSER_PROBLEM_COOLDOWN_SECONDS", 3600), 0
            ),
            parser_network_cooldown_seconds=max(_as_int("PARSER_NETWORK_COOLDOWN_SECONDS", 900), 0),
            user_agent=os.getenv(
                "USER_AGENT",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            ),
            use_playwright=_as_bool(os.getenv("USE_PLAYWRIGHT"), False),
            playwright_fallback=_as_bool(os.getenv("PLAYWRIGHT_FALLBACK"), False),
            playwright_headless=_as_bool(os.getenv("PLAYWRIGHT_HEADLESS"), True),
            environment=os.getenv("ENVIRONMENT", "prod").strip().lower(),
            prod_telegram_bot_token_hash=os.getenv("PROD_TELEGRAM_BOT_TOKEN_HASH") or None,
            yookassa_shop_id=os.getenv("YOOKASSA_SHOP_ID") or None,
            yookassa_secret_key=os.getenv("YOOKASSA_SECRET_KEY") or None,
            yookassa_return_url=os.getenv("YOOKASSA_RETURN_URL") or None,
            subscription_price_rub=max(_as_int("SUBSCRIPTION_PRICE_RUB", 199), 1),
            subscription_period_days=max(_as_int("SUBSCRIPTION_PERIOD_DAYS", 31), 1),
            trial_days=max(_as_int("TRIAL_DAYS", 7), 0),
        )

    def validate_environment(self) -> None:
        if self.environment not in {"dev", "prod"}:
            raise ConfigError("ENVIRONMENT must be 'dev' or 'prod'")
        if self.environment != "dev":
            return
        if not self.telegram_bot_token or not self.prod_telegram_bot_token_hash:
            return
        token_hash = hashlib.sha256(self.telegram_bot_token.encode()).hexdigest()
        if token_hash == self.prod_telegram_bot_token_hash.strip().lower():
            raise ConfigError(
                "Dev environment is configured with the production Telegram bot token"
            )

    def require_telegram(self) -> None:
        if self.dry_run:
            return
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required unless DRY_RUN=true")
        if not self.telegram_chat_id:
            raise ConfigError("TELEGRAM_CHAT_ID is required unless DRY_RUN=true")

    def with_runtime_overrides(self, values: dict[str, str]) -> "Settings":
        overrides: dict[str, object] = {}
        for key, value in values.items():
            if key == "cian_search_url":
                overrides[key] = value or None
            elif key == "cian_use_generated_url":
                overrides[key] = _as_bool(value, True)
            elif key in {"cian_city", "cian_region_id", "cian_rent_type", "cian_sort_by"}:
                stripped = value.strip()
                overrides[key] = stripped or None if key == "cian_region_id" else stripped
            elif key == "cian_rooms":
                overrides[key] = _as_rooms(value)
            elif key in {"cian_min_price", "cian_max_price"}:
                overrides[key] = _parse_optional_int(key, value)
            elif key == "cian_polygon":
                overrides[key] = value.strip() or None
            elif key == "cian_area_label":
                overrides[key] = value.strip() or None
            else:
                raise ConfigError(f"Unknown runtime setting: {key}")
        return replace(self, **overrides)
