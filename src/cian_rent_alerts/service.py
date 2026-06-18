from __future__ import annotations

import logging
import random
import threading
import time
import hashlib
import json
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Mapping

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from .analytics import (
    EV_CAPTCHA_ERROR,
    EV_TELEGRAM_SEND_ERROR,
    EV_TRIAL_EXPIRED,
    EV_WEBHOOK_ERROR,
    event_for_error_type,
)
from .cian_url import build_cian_search_url
from .config import ConfigError, Settings
from .db import ListingStore
from .models import Listing
from .notifier import TelegramNotifier, TelegramSendPolicy, send_listings_sync, send_message_sync
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
_NO_NEW_LISTINGS_NUDGE_AFTER = timedelta(hours=24)
_PAYMENT_REQUIRED_NOTICE_COOLDOWN = timedelta(hours=24)
_MONITORING_ALERT_STATE_KEY = "monitoring:last_alert"


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
        proxy_server=settings.cian_proxy_server,
        proxy_username=settings.cian_proxy_username,
        proxy_password=settings.cian_proxy_password,
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
            deal_type=settings.cian_deal_type,
            polygon=settings.cian_polygon,
        )
    if not settings.cian_search_url:
        raise ConfigError("CIAN_SEARCH_URL is required when CIAN_USE_GENERATED_URL=false")
    return settings.cian_search_url


def search_fingerprint(settings: Settings) -> str:
    url = build_search_url(settings)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def settings_with_search(settings: Settings, search: Mapping[str, object]) -> Settings:
    return replace(
        settings,
        cian_search_url=_optional_str(search.get("manual_url")),
        cian_use_generated_url=_as_bool(search.get("use_generated_url")),
        cian_city=str(search["city"]),
        cian_region_id=_optional_str(search.get("region_id")),
        cian_deal_type=str(search.get("deal_type") or "rent"),
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
    allow_global_fallback: bool = False,
) -> int:
    return run_check_result(
        settings,
        only_chat_id=only_chat_id,
        only_search_id=only_search_id,
        fail_if_running=fail_if_running,
        allow_global_fallback=allow_global_fallback,
    ).notifications_sent


def run_check_result(
    settings: Settings,
    *,
    only_chat_id: str | None = None,
    only_search_id: int | None = None,
    fail_if_running: bool = False,
    allow_global_fallback: bool = False,
) -> CheckRunResult:
    if not _CHECK_LOCK.acquire(blocking=False):
        logger.info("Check is already running, skipping new request")
        if fail_if_running:
            raise CheckAlreadyRunning("Проверка уже выполняется")
        return CheckRunResult(run_id=None, notifications_sent=0)
    try:
        return _run_check(
            settings,
            only_chat_id=only_chat_id,
            only_search_id=only_search_id,
            allow_global_fallback=allow_global_fallback,
        )
    finally:
        _CHECK_LOCK.release()


def run_monitoring(settings: Settings) -> None:
    if settings.dry_run or not settings.telegram_bot_token or not settings.admin_telegram_ids:
        return

    store = ListingStore(settings.database_path, settings.database_url)
    store.init()
    problems = _monitoring_problems(store, settings)
    if not problems:
        store.delete_runtime_setting(_MONITORING_ALERT_STATE_KEY)
        return
    if not _should_send_monitoring_alert(store, settings, problems):
        return

    text = "\n".join(["FlatPulse: мониторинг", *[f"- {problem}" for problem in problems]])
    for chat_id in settings.admin_telegram_ids:
        try:
            notifier = _telegram_notifier(settings, chat_id)
            send_message_sync(notifier, text)
        except Exception:
            logger.exception("Failed to send monitoring alert to chat_id=%s", chat_id)


def _telegram_notifier(settings: Settings, chat_id: str) -> TelegramNotifier:
    return TelegramNotifier(
        token=settings.telegram_bot_token or "",
        chat_id=chat_id,
        send_policy=TelegramSendPolicy(
            rate_limit_seconds=settings.telegram_rate_limit_seconds,
            retry_attempts=settings.telegram_retry_attempts,
            retry_backoff_seconds=settings.telegram_retry_backoff_seconds,
        ),
    )


def _monitoring_problems(store: ListingStore, settings: Settings) -> list[str]:
    problems: list[str] = []
    active_searches = store.searches_count_by_status(active=True)
    if active_searches > 0:
        last_success = store.last_successful_check_run()
        max_age = timedelta(minutes=settings.monitoring_max_success_age_minutes)
        if last_success is None:
            problems.append("нет успешных проверок при активных поисках")
        else:
            finished_at = _parse_state_datetime(
                str(last_success.get("finished_at") or last_success.get("started_at") or "")
            )
            if finished_at is None or datetime.now(UTC) - finished_at > max_age:
                problems.append(
                    "последняя успешная проверка старше "
                    f"{settings.monitoring_max_success_age_minutes} мин"
                )

    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
    events = store.analytics_events_summary_since(since)
    captcha_errors = events.get(EV_CAPTCHA_ERROR, 0)
    telegram_errors = events.get(EV_TELEGRAM_SEND_ERROR, 0)
    webhook_errors = events.get(EV_WEBHOOK_ERROR, 0)

    if (
        settings.monitoring_captcha_error_threshold > 0
        and captcha_errors >= settings.monitoring_captcha_error_threshold
    ):
        problems.append(f"captcha_errors за 24ч: {captcha_errors}")
    if (
        settings.monitoring_telegram_error_threshold > 0
        and telegram_errors >= settings.monitoring_telegram_error_threshold
    ):
        problems.append(f"telegram_send_errors за 24ч: {telegram_errors}")
    if (
        settings.monitoring_webhook_error_threshold > 0
        and webhook_errors >= settings.monitoring_webhook_error_threshold
    ):
        problems.append(f"webhook_errors за 24ч: {webhook_errors}")

    return problems


def _should_send_monitoring_alert(
    store: ListingStore,
    settings: Settings,
    problems: list[str],
) -> bool:
    now = datetime.now(UTC)
    signature = hashlib.sha256("\n".join(problems).encode("utf-8")).hexdigest()
    raw_state = store.get_runtime_settings().get(_MONITORING_ALERT_STATE_KEY)
    if raw_state:
        try:
            state = json.loads(raw_state)
            last_signature = str(state.get("signature") or "")
            last_sent_at = _parse_state_datetime(str(state.get("sent_at") or ""))
        except Exception:
            last_signature = ""
            last_sent_at = None
        if (
            last_signature == signature
            and last_sent_at is not None
            and now - last_sent_at < timedelta(seconds=settings.monitoring_alert_cooldown_seconds)
        ):
            return False

    store.set_runtime_setting(
        _MONITORING_ALERT_STATE_KEY,
        json.dumps(
            {
                "signature": signature,
                "sent_at": now.isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    return True


def _run_check(
    settings: Settings,
    *,
    only_chat_id: str | None = None,
    only_search_id: int | None = None,
    allow_global_fallback: bool = False,
) -> CheckRunResult:
    store = ListingStore(settings.database_path, settings.database_url)
    store.init()
    run_id = store.start_check_run()
    listings_found = 0
    listings_saved = 0
    new_listings = 0
    notifications_sent = 0
    active_searches_count = 0
    unique_search_groups = 0
    cian_fetches = 0
    shared_group_hits = 0
    search_errors: list[str] = []

    try:
        searches = _filter_searches(
            store.active_searches(),
            only_chat_id=only_chat_id,
            only_search_id=only_search_id,
        )
        active_searches_count = len(searches)
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
            if not allow_global_fallback or not settings.telegram_chat_id:
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

        search_groups: dict[str, list[tuple[dict[str, object], Settings]]] = {}
        for search in searches:
            try:
                search_settings = settings_with_search(settings, search)
                fingerprint = search_fingerprint(search_settings)
                search_groups.setdefault(fingerprint, []).append((search, search_settings))
            except Exception as exc:
                search_id = int(search["id"])
                user_id = int(search["user_id"])
                chat_id = str(search["telegram_chat_id"])
                error_type = classify_check_error(exc)
                _record_check_error_event(
                    store,
                    error_type,
                    user_id=user_id,
                    search_id=search_id,
                )
                store.record_search_error(
                    search_id,
                    error_type=error_type,
                    cooldown_until=_cooldown_until_for_error(settings, error_type),
                )
                error = f"{error_type}: search_id={search_id} chat_id={chat_id}: {exc}"
                search_errors.append(error)
                logger.exception("Search check failed before grouping: %s", error)

        unique_search_groups = len(search_groups)
        shared_group_hits = sum(max(len(group) - 1, 0) for group in search_groups.values())

        for index, group in enumerate(search_groups.values()):
            group_settings = group[0][1]
            try:
                cian_fetches += 1
                listings = fetch_listings(group_settings)
                listings_saved += store.upsert_many(listings)
                logger.info(
                    "Fetched %s listings for search_group size=%s fingerprint=%s city=%s",
                    len(listings),
                    len(group),
                    search_fingerprint(group_settings)[:12],
                    group_settings.cian_city,
                )
            except Exception as exc:
                error_type = classify_check_error(exc)
                for search, _search_settings in group:
                    search_id = int(search["id"])
                    user_id = int(search["user_id"])
                    chat_id = str(search["telegram_chat_id"])
                    _record_check_error_event(
                        store,
                        error_type,
                        user_id=user_id,
                        search_id=search_id,
                    )
                    store.record_search_error(
                        search_id,
                        error_type=error_type,
                        cooldown_until=_cooldown_until_for_error(settings, error_type),
                    )
                    error = f"{error_type}: search_id={search_id} chat_id={chat_id}: {exc}"
                    search_errors.append(error)
                    logger.exception("Search group check failed: %s", error)
                _delay_between_searches(
                    settings,
                    current_index=index,
                    total_count=len(search_groups),
                    only_chat_id=only_chat_id,
                )
                continue

            for search, search_settings in group:
                search_id = int(search["id"])
                chat_id = str(search["telegram_chat_id"])
                try:
                    was_initialized = bool(search.get("initialized_at"))

                    listings_found += len(listings)
                    store.record_search_success(search_id, initialize=not was_initialized)
                    _maybe_record_trial_expired(store, search)
                    if _sync_search_access_status(store, search):
                        _maybe_notify_payment_required(store, search_settings, search)
                        continue
                    logger.info(
                        "Applied %s listings for search_id=%s chat_id=%s",
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
                        _remember_search_activity_checkpoint(store, search)
                        logger.info(
                            "Seeded %s existing listings for search_id=%s",
                            len(listings),
                            search_id,
                        )
                        continue

                    unseen = store.unseen_listings_for_search(search_id, listings)
                    new_listings += len(unseen)
                    if not unseen:
                        _maybe_notify_no_new_listings(store, search_settings, search)
                        continue

                    if not search_settings.dry_run and not store.user_has_active_access(
                        int(search["user_id"])
                    ):
                        _maybe_notify_payment_required(store, search_settings, search)
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
                        _remember_search_activity_checkpoint(store, search)
                        continue

                    notifier = _telegram_notifier(search_settings, chat_id)
                    sent_ids = send_listings_sync(notifier, unseen)
                    notifications_sent += len(sent_ids)
                    store.mark_many_search_listings_seen(
                        search_id=search_id,
                        cian_ids=sent_ids,
                        sent=True,
                    )
                    if sent_ids:
                        _remember_search_activity_checkpoint(store, search)
                        _delay_after_telegram_send(
                            search_settings,
                            sent_count=len(sent_ids),
                            only_chat_id=only_chat_id,
                        )
                except Exception as exc:
                    error_type = classify_check_error(exc)
                    _record_check_error_event(
                        store,
                        error_type,
                        user_id=int(search["user_id"]),
                        search_id=search_id,
                    )
                    store.record_search_error(
                        search_id,
                        error_type=error_type,
                        cooldown_until=_cooldown_until_for_error(settings, error_type),
                    )
                    error = f"{error_type}: search_id={search_id} chat_id={chat_id}: {exc}"
                    search_errors.append(error)
                    logger.exception("Search check failed: %s", error)

            _delay_between_searches(
                settings,
                current_index=index,
                total_count=len(search_groups),
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
            active_searches=active_searches_count,
            unique_search_groups=unique_search_groups,
            cian_fetches=cian_fetches,
            shared_group_hits=shared_group_hits,
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
        _record_check_error_event(store, classify_check_error(exc))
        store.finish_check_run(
            run_id,
            status="failed",
            listings_found=listings_found,
            listings_saved=listings_saved,
            new_listings=new_listings,
            notifications_sent=notifications_sent,
            active_searches=active_searches_count,
            unique_search_groups=unique_search_groups,
            cian_fetches=cian_fetches,
            shared_group_hits=shared_group_hits,
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
    notifier = _telegram_notifier(effective_settings, effective_settings.telegram_chat_id or "")
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


def _record_check_error_event(
    store: ListingStore,
    error_type: str,
    *,
    user_id: int | None = None,
    search_id: int | None = None,
) -> None:
    try:
        store.record_event(
            event_for_error_type(error_type),
            user_id=user_id,
            search_id=search_id,
        )
    except Exception:
        logger.exception("Failed to record check error event type=%s", error_type)


def _cooldown_until_for_error(settings: Settings, error_type: str) -> str | None:
    if error_type == "captcha":
        seconds = settings.parser_captcha_cooldown_seconds
    elif error_type == "empty_parse":
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

    delay_seconds = settings.search_check_delay_seconds
    if settings.search_check_delay_jitter_seconds > 0:
        delay_seconds += random.uniform(0, settings.search_check_delay_jitter_seconds)

    logger.info(
        "Waiting %.1f seconds before next search check",
        delay_seconds,
    )
    time.sleep(delay_seconds)


def _delay_after_telegram_send(
    settings: Settings,
    *,
    sent_count: int,
    only_chat_id: str | None,
) -> None:
    if only_chat_id is not None:
        return
    if sent_count <= 0 or settings.telegram_send_delay_seconds <= 0:
        return

    logger.info(
        "Waiting %s seconds after Telegram send batch",
        settings.telegram_send_delay_seconds,
    )
    time.sleep(settings.telegram_send_delay_seconds)


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
            notifier = _telegram_notifier(settings, chat_id)
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


def _remember_search_activity_checkpoint(
    store: ListingStore,
    search: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    user_id = int(search["user_id"])
    search_id = int(search["id"])
    store.set_user_state(
        user_id,
        _last_new_listing_state_key(search_id),
        now.isoformat(timespec="seconds"),
    )
    store.delete_user_state(user_id, _no_new_nudge_state_key(search_id))


def _maybe_notify_no_new_listings(
    store: ListingStore,
    settings: Settings,
    search: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> None:
    if settings.dry_run or not settings.telegram_bot_token:
        return

    now = now or datetime.now(UTC)
    user_id = int(search["user_id"])
    search_id = int(search["id"])
    last_activity = _parse_state_datetime(
        store.get_user_state(user_id, _last_new_listing_state_key(search_id))
    )
    if last_activity is None:
        _remember_search_activity_checkpoint(store, search, now=now)
        return
    if now - last_activity < _NO_NEW_LISTINGS_NUDGE_AFTER:
        return

    last_nudge = _parse_state_datetime(
        store.get_user_state(user_id, _no_new_nudge_state_key(search_id))
    )
    if last_nudge is not None and now - last_nudge < _NO_NEW_LISTINGS_NUDGE_AFTER:
        return

    try:
        notifier = _telegram_notifier(settings, str(search["telegram_chat_id"]))
        send_message_sync(
            notifier,
            _no_new_listings_nudge_text(),
            reply_markup=_no_new_listings_nudge_keyboard(),
        )
        store.set_user_state(
            user_id,
            _no_new_nudge_state_key(search_id),
            now.isoformat(timespec="seconds"),
        )
    except Exception:
        logger.exception("Failed to send no-new-listings nudge for search_id=%s", search_id)


def _maybe_notify_payment_required(
    store: ListingStore,
    settings: Settings,
    search: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> None:
    if settings.dry_run or not settings.telegram_bot_token:
        return

    now = now or datetime.now(UTC)
    user_id = int(search["user_id"])
    search_id = int(search["id"])
    last_notice = _parse_state_datetime(
        store.get_user_state(user_id, _payment_required_notice_state_key(search_id))
    )
    if last_notice is not None and now - last_notice < _PAYMENT_REQUIRED_NOTICE_COOLDOWN:
        return

    try:
        notifier = _telegram_notifier(settings, str(search["telegram_chat_id"]))
        send_message_sync(
            notifier,
            _payment_required_text(settings),
            reply_markup=_payment_required_keyboard(),
        )
        store.set_user_state(
            user_id,
            _payment_required_notice_state_key(search_id),
            now.isoformat(timespec="seconds"),
        )
        _maybe_record_trial_expired(store, search)
    except Exception:
        logger.exception("Failed to send payment-required notice for search_id=%s", search_id)


def _maybe_record_trial_expired(store: ListingStore, search: Mapping[str, object]) -> None:
    user_id = int(search["user_id"])
    search_id = int(search["id"])
    user = store.get_user(user_id)
    if user is None or not user.get("trial_started_at"):
        return
    trial_ends_at = _parse_state_datetime(str(user.get("trial_ends_at") or ""))
    if trial_ends_at is None or trial_ends_at > datetime.now(UTC):
        return
    if store.user_has_active_access(user_id):
        return
    state_key = _trial_expired_event_state_key(search_id)
    if store.get_user_state(user_id, state_key):
        return
    store.record_event(EV_TRIAL_EXPIRED, user_id=user_id, search_id=search_id)
    store.set_user_state(user_id, state_key, datetime.now(UTC).isoformat(timespec="seconds"))


def _sync_search_access_status(store: ListingStore, search: Mapping[str, object]) -> bool:
    user_id = int(search["user_id"])
    search_id = int(search["id"])
    user = store.get_user(user_id)
    if user is None:
        return False
    if store.user_has_active_access(user_id):
        return False
    if not user.get("trial_started_at") and not user.get("paid_until"):
        return False
    store.update_search(search_id, is_active=False, initialized_at=None)
    return True


def _last_new_listing_state_key(search_id: int) -> str:
    return f"last_new_listing_at:{search_id}"


def _no_new_nudge_state_key(search_id: int) -> str:
    return f"no_new_nudge_at:{search_id}"


def _payment_required_notice_state_key(search_id: int) -> str:
    return f"payment_required_notice_at:{search_id}"


def _trial_expired_event_state_key(search_id: int) -> str:
    return f"trial_expired_event_recorded:{search_id}"


def _parse_state_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _no_new_listings_nudge_text() -> str:
    return "\n".join(
        [
            "📉 За последние 24 часа по вашим параметрам не появилось ни одного нового объявления.",
            "",
            "Хотите получать больше вариантов?",
            "",
            "Попробуйте немного расширить поиск.",
        ]
    )


def _no_new_listings_nudge_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⚙️ Настроить фильтры", callback_data="cfg:setup")]]
    )


def _payment_required_text(settings: Settings) -> str:
    return "\n".join(
        [
            "⏳ Доступ к уведомлениям закончился.",
            "",
            "FlatPulse уже знает ваши параметры поиска, но новые уведомления сейчас на паузе.",
            "",
            f"Подписка стоит {settings.subscription_price_rub} ₽ в месяц.",
            "После оплаты бот продолжит присылать только новые подходящие квартиры.",
            "",
            "Без автосписаний: следующий месяц оплачивается вручную.",
        ]
    )


def _payment_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("💳 Подписка", callback_data="cfg:subscribe")]]
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
