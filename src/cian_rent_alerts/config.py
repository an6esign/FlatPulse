from __future__ import annotations

import os
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
    database_path: Path
    check_interval_seconds: int
    listing_limit: int
    dry_run: bool
    request_timeout_seconds: int
    user_agent: str
    use_playwright: bool
    playwright_headless: bool

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
            database_path=Path(os.getenv("DATABASE_PATH", "data/listings.sqlite3")),
            check_interval_seconds=max(_as_int("CHECK_INTERVAL_SECONDS", 600), 60),
            listing_limit=max(_as_int("LISTING_LIMIT", 50), 1),
            dry_run=_as_bool(os.getenv("DRY_RUN"), False),
            request_timeout_seconds=max(_as_int("REQUEST_TIMEOUT_SECONDS", 20), 1),
            user_agent=os.getenv(
                "USER_AGENT",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            ),
            use_playwright=_as_bool(os.getenv("USE_PLAYWRIGHT"), False),
            playwright_headless=_as_bool(os.getenv("PLAYWRIGHT_HEADLESS"), True),
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
