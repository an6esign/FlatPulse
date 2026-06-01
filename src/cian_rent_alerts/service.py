from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Mapping

import requests
from telegram.error import TelegramError

from .cian_url import build_cian_search_url
from .config import ConfigError, Settings
from .db import ListingStore
from .models import Listing
from .notifier import TelegramNotifier, send_listings_sync, send_message_sync
from .scraper import (
    CaptchaError,
    EmptyParseError,
    NetworkFetchError,
    PlaywrightCianScraper,
    RequestsCianScraper,
    ScraperConfig,
    scrape,
)

logger = logging.getLogger(__name__)
_CHECK_LOCK = threading.Lock()


class CheckAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckRunResult:
    run_id: int | None
    notifications_sent: int


def build_scraper(settings: Settings) -> RequestsCianScraper | PlaywrightCianScraper:
    config = build_scraper_config(settings)
    if settings.use_playwright:
        return PlaywrightCianScraper(config, headless=settings.playwright_headless)
    return RequestsCianScraper(config)


def build_playwright_scraper(settings: Settings) -> PlaywrightCianScraper:
    return PlaywrightCianScraper(
        build_scraper_config(settings),
        headless=settings.playwright_headless,
    )


def build_scraper_config(settings: Settings) -> ScraperConfig:
    search_url = build_search_url(settings)
    return ScraperConfig(
        search_url=search_url,
        user_agent=settings.user_agent,
        timeout_seconds=settings.request_timeout_seconds,
        limit=settings.listing_limit,
        debug_dir=settings.parser_debug_dir,
    )


def fetch_listings(settings: Settings) -> list[Listing]:
    listings = scrape_with_retries(settings)
    return filter_recent_listings(
        listings,
        max_age_days=settings.listing_max_age_days,
    )


def scrape_with_retries(settings: Settings) -> list[Listing]:
    attempts = max(settings.parser_retry_attempts, 1)
    for attempt in range(1, attempts + 1):
        try:
            return scrape_with_fallback(settings)
        except Exception as exc:
            if not _is_retryable_network_error(exc) or attempt >= attempts:
                raise

            logger.warning(
                "CIAN scrape network error, retrying attempt %s/%s parser=%s city=%s url=%s: %s",
                attempt + 1,
                attempts,
                _parser_name(settings),
                settings.cian_city,
                _safe_search_url(settings),
                exc,
            )
            if settings.parser_retry_backoff_seconds > 0:
                time.sleep(settings.parser_retry_backoff_seconds * attempt)

    raise RuntimeError("Unexpected parser retry state")


def scrape_with_fallback(settings: Settings) -> list[Listing]:
    scraper = build_scraper(settings)
    try:
        return scrape(scraper, settings.listing_limit)
    except (CaptchaError, EmptyParseError) as exc:
        if settings.use_playwright or not settings.playwright_fallback:
            raise

        logger.info("Retrying CIAN with Playwright after %s", exc.__class__.__name__)
        playwright_scraper = build_playwright_scraper(settings)
        try:
            return scrape(playwright_scraper, settings.listing_limit)
        except RuntimeError as fallback_exc:
            if "playwright is not installed" in str(fallback_exc).lower():
                logger.warning("Playwright fallback is enabled but unavailable: %s", fallback_exc)
                raise exc from fallback_exc
            raise


def _is_retryable_network_error(exc: Exception) -> bool:
    if isinstance(exc, NetworkFetchError):
        return True
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    cause = exc.__cause__
    return isinstance(cause, (requests.Timeout, requests.ConnectionError))


def _parser_name(settings: Settings) -> str:
    if settings.use_playwright:
        return "playwright"
    if settings.playwright_fallback:
        return "requests+playwright_fallback"
    return "requests"


def _safe_search_url(settings: Settings) -> str:
    try:
        return build_search_url(settings)
    except ConfigError:
        return "unavailable"


def build_search_url(settings: Settings) -> str:
    if settings.cian_use_generated_url:
        return build_cian_search_url(
            city=settings.cian_city,
            region_id=settings.cian_region_id,
            rooms=settings.cian_rooms,
            min_price=settings.cian_min_price,
            max_price=settings.cian_max_price,
            rent_type=settings.cian_rent_type,
            sort_by=settings.cian_sort_by,
            polygon=settings.cian_polygon,
        )
    if not settings.cian_search_url:
        raise ConfigError("CIAN_SEARCH_URL is required when CIAN_USE_GENERATED_URL=false")
    return settings.cian_search_url


def settings_with_search(settings: Settings, search: Mapping[str, object]) -> Settings:
    return replace(
        settings,
        cian_search_url=_optional_str(search.get("manual_url")),
        cian_use_generated_url=_as_bool(search.get("use_generated_url")),
        cian_city=str(search["city"]),
        cian_region_id=_optional_str(search.get("region_id")),
        cian_rooms=_rooms_from_search(search.get("rooms")),
        cian_min_price=_optional_int(search.get("min_price")),
        cian_max_price=_optional_int(search.get("max_price")),
        cian_rent_type=str(search["rent_type"]),
        cian_sort_by=str(search["sort_by"]),
        cian_polygon=_optional_str(search.get("polygon")),
        cian_area_label=_optional_str(search.get("area_label")),
    )


def run_check(
    settings: Settings,
    *,
    only_chat_id: str | None = None,
    only_search_id: int | None = None,
    fail_if_running: bool = False,
) -> int:
    return run_check_result(
        settings,
        only_chat_id=only_chat_id,
        only_search_id=only_search_id,
        fail_if_running=fail_if_running,
    ).notifications_sent


def run_check_result(
    settings: Settings,
    *,
    only_chat_id: str | None = None,
    only_search_id: int | None = None,
    fail_if_running: bool = False,
) -> CheckRunResult:
    if not _CHECK_LOCK.acquire(blocking=False):
        logger.info("Check is already running, skipping new request")
        if fail_if_running:
            raise CheckAlreadyRunning("Проверка уже выполняется")
        return CheckRunResult(run_id=None, notifications_sent=0)
    try:
        return _run_check(settings, only_chat_id=only_chat_id, only_search_id=only_search_id)
    finally:
        _CHECK_LOCK.release()


def _run_check(
    settings: Settings,
    *,
    only_chat_id: str | None = None,
    only_search_id: int | None = None,
) -> CheckRunResult:
    store = ListingStore(settings.database_path, settings.database_url)
    store.init()
    run_id = store.start_check_run()
    listings_found = 0
    listings_saved = 0
    new_listings = 0
    notifications_sent = 0
    search_errors: list[str] = []

    try:
        searches = _filter_searches(
            store.active_searches(),
            only_chat_id=only_chat_id,
            only_search_id=only_search_id,
        )
        if not searches:
            if store.searches_count() > 0:
                logger.info("No active searches to check")
                store.finish_check_run(
                    run_id,
                    status="success",
                    listings_found=0,
                    listings_saved=0,
                    new_listings=0,
                    notifications_sent=0,
                )
                return CheckRunResult(run_id=run_id, notifications_sent=0)
            if only_chat_id is not None:
                logger.info("No active search for chat_id=%s", only_chat_id)
                store.finish_check_run(
                    run_id,
                    status="success",
                    listings_found=0,
                    listings_saved=0,
                    new_listings=0,
                    notifications_sent=0,
                )
                return CheckRunResult(run_id=run_id, notifications_sent=0)
            if not settings.telegram_chat_id:
                logger.info("No active searches to check")
                store.finish_check_run(
                    run_id,
                    status="success",
                    listings_found=0,
                    listings_saved=0,
                    new_listings=0,
                    notifications_sent=0,
                )
                return CheckRunResult(run_id=run_id, notifications_sent=0)
            result = _run_global_check(store, settings)
            store.finish_check_run(run_id, status="success", **result)
            return CheckRunResult(
                run_id=run_id,
                notifications_sent=result["notifications_sent"],
            )

        if not settings.dry_run and not settings.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required unless DRY_RUN=true")

        for index, search in enumerate(searches):
            search_id = int(search["id"])
            chat_id = str(search["telegram_chat_id"])
            try:
                search_settings = settings_with_search(settings, search)
                was_initialized = bool(search.get("initialized_at"))

                listings = fetch_listings(search_settings)
                listings_found += len(listings)
                listings_saved += store.upsert_many(listings)
                store.record_search_success(search_id, initialize=not was_initialized)
                logger.info(
                    "Fetched %s listings for search_id=%s chat_id=%s",
                    len(listings),
                    search_id,
                    chat_id,
                )

                if not was_initialized:
                    store.mark_many_search_listings_seen(
                        search_id=search_id,
                        cian_ids=[listing.cian_id for listing in listings],
                        sent=False,
                    )
                    logger.info(
                        "Seeded %s existing listings for search_id=%s", len(listings), search_id
                    )
                    continue

                unseen = store.unseen_listings_for_search(search_id, listings)
                new_listings += len(unseen)
                if not unseen:
                    continue

                if search_settings.dry_run:
                    for listing in unseen:
                        logger.info(
                            "DRY_RUN search_id=%s listing:\n%s",
                            search_id,
                            listing.format_message(),
                        )
                    store.mark_many_search_listings_seen(
                        search_id=search_id,
                        cian_ids=[listing.cian_id for listing in unseen],
                        sent=False,
                    )
                    continue

                notifier = TelegramNotifier(
                    token=search_settings.telegram_bot_token or "",
                    chat_id=chat_id,
                )
                sent_ids = send_listings_sync(notifier, unseen)
                notifications_sent += len(sent_ids)
                store.mark_many_search_listings_seen(
                    search_id=search_id,
                    cian_ids=sent_ids,
                    sent=True,
                )
            except Exception as exc:
                error_type = classify_check_error(exc)
                store.record_search_error(
                    search_id,
                    error_type=error_type,
                    cooldown_until=_cooldown_until_for_error(settings, error_type),
                )
                error = f"{error_type}: search_id={search_id} chat_id={chat_id}: {exc}"
                search_errors.append(error)
                logger.exception("Search check failed: %s", error)
            finally:
                _delay_between_searches(
                    settings,
                    current_index=index,
                    total_count=len(searches),
                    only_chat_id=only_chat_id,
                )

        status = "partial" if search_errors else "success"
        error_text = "\n".join(search_errors) if search_errors else None
        store.finish_check_run(
            run_id,
            status=status,
            listings_found=listings_found,
            listings_saved=listings_saved,
            new_listings=new_listings,
            notifications_sent=notifications_sent,
            error=error_text,
        )
        if error_text is not None and _should_notify_admins_about_check_problem(
            status=status,
            error=error_text,
        ):
            _notify_admins_about_check_problem(
                settings,
                status=status,
                run_id=run_id,
                error=error_text,
            )
        return CheckRunResult(run_id=run_id, notifications_sent=notifications_sent)
    except Exception as exc:
        store.finish_check_run(
            run_id,
            status="failed",
            listings_found=listings_found,
            listings_saved=listings_saved,
            new_listings=new_listings,
            notifications_sent=notifications_sent,
            error=f"{classify_check_error(exc)}: {exc}",
        )
        _notify_admins_about_check_problem(
            settings,
            status="failed",
            run_id=run_id,
            error=f"{classify_check_error(exc)}: {exc}",
        )
        raise


def _run_global_check(store: ListingStore, settings: Settings) -> dict[str, int]:
    effective_settings = settings.with_runtime_overrides(store.get_runtime_settings())
    listings_found = 0
    listings_saved = 0
    new_listings = 0
    notifications_sent = 0

    listings = fetch_listings(effective_settings)
    listings_found = len(listings)
    listings_saved = store.upsert_many(listings)
    logger.info("Fetched %s listings, saved %s records", listings_found, listings_saved)

    unsent = store.unsent(effective_settings.listing_limit)
    new_listings = len(unsent)
    if not unsent:
        logger.info("No new listings to notify")
        return {
            "listings_found": listings_found,
            "listings_saved": listings_saved,
            "new_listings": new_listings,
            "notifications_sent": notifications_sent,
        }

    if effective_settings.dry_run:
        for listing in unsent:
            logger.info("DRY_RUN listing:\n%s", listing.format_message())
        return {
            "listings_found": listings_found,
            "listings_saved": listings_saved,
            "new_listings": new_listings,
            "notifications_sent": new_listings,
        }

    effective_settings.require_telegram()
    notifier = TelegramNotifier(
        token=effective_settings.telegram_bot_token or "",
        chat_id=effective_settings.telegram_chat_id or "",
    )
    sent_ids = send_listings_sync(notifier, unsent)
    notifications_sent = len(sent_ids)
    for cian_id in sent_ids:
        store.mark_sent(cian_id)

    return {
        "listings_found": listings_found,
        "listings_saved": listings_saved,
        "new_listings": new_listings,
        "notifications_sent": notifications_sent,
    }


def filter_recent_listings(listings: list[Listing], *, max_age_days: int) -> list[Listing]:
    if max_age_days <= 0:
        return list(listings)

    threshold = date.today() - timedelta(days=max_age_days)
    recent = []
    for listing in listings:
        published_at = getattr(listing, "raw", {}).get("published_at")
        if not published_at:
            recent.append(listing)
            continue
        try:
            published_date = date.fromisoformat(str(published_at)[:10])
        except ValueError:
            recent.append(listing)
            continue
        if published_date >= threshold:
            recent.append(listing)
    return recent


def classify_check_error(exc: Exception) -> str:
    if isinstance(exc, ConfigError):
        return "config"
    if isinstance(exc, TelegramError):
        return "telegram"
    if isinstance(exc, CaptchaError):
        return "captcha"
    if isinstance(exc, EmptyParseError):
        return "empty_parse"
    if isinstance(exc, NetworkFetchError):
        return "network"

    cause = exc.__cause__
    if isinstance(cause, requests.RequestException) or isinstance(exc, requests.RequestException):
        return "network"

    message = str(exc).lower()
    if "captcha" in message or "access-check" in message:
        return "captcha"
    if "no listings" in message or "empty" in message:
        return "empty_parse"
    if "failed to fetch" in message or "timeout" in message or "connection" in message:
        return "network"
    if "telegram" in message or "bot" in message or "chat not found" in message:
        return "telegram"
    return "unknown"


def _cooldown_until_for_error(settings: Settings, error_type: str) -> str | None:
    if error_type in {"captcha", "empty_parse"}:
        seconds = settings.parser_problem_cooldown_seconds
    elif error_type == "network":
        seconds = settings.parser_network_cooldown_seconds
    else:
        seconds = 0

    if seconds <= 0:
        return None
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _delay_between_searches(
    settings: Settings,
    *,
    current_index: int,
    total_count: int,
    only_chat_id: str | None,
) -> None:
    if only_chat_id is not None:
        return
    if current_index >= total_count - 1:
        return
    if settings.search_check_delay_seconds <= 0:
        return

    logger.info(
        "Waiting %s seconds before next search check",
        settings.search_check_delay_seconds,
    )
    time.sleep(settings.search_check_delay_seconds)


def _notify_admins_about_check_problem(
    settings: Settings,
    *,
    status: str,
    run_id: int,
    error: str,
) -> None:
    if settings.dry_run or not settings.telegram_bot_token or not settings.admin_telegram_ids:
        return

    text = _format_admin_problem_message(status=status, run_id=run_id, error=error)
    for chat_id in settings.admin_telegram_ids:
        try:
            notifier = TelegramNotifier(
                token=settings.telegram_bot_token,
                chat_id=chat_id,
            )
            send_message_sync(notifier, text)
        except Exception:
            logger.exception("Failed to send admin problem notification to chat_id=%s", chat_id)


def _should_notify_admins_about_check_problem(*, status: str, error: str) -> bool:
    if status == "failed":
        return True
    if status != "partial":
        return False
    return not all(
        error_line.startswith(("empty_parse:", "captcha:"))
        for error_line in error.splitlines()
        if error_line
    )


def _format_admin_problem_message(*, status: str, run_id: int, error: str) -> str:
    return "\n".join(
        [
            "FlatPulse: проблема проверки",
            f"Run ID: {run_id}",
            f"Status: {status}",
            f"Error: {error[:1200]}",
        ]
    )


def _filter_searches(
    searches: list[dict[str, object]],
    *,
    only_chat_id: str | None,
    only_search_id: int | None,
) -> list[dict[str, object]]:
    filtered = searches
    if only_chat_id is not None:
        filtered = [
            search for search in filtered if str(search["telegram_chat_id"]) == str(only_chat_id)
        ]
    if only_search_id is not None:
        filtered = [search for search in filtered if int(search["id"]) == only_search_id]
    return filtered


def _rooms_from_search(value: object) -> tuple[str, ...]:
    if value is None or str(value).strip() == "":
        return ("all",)
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
