from __future__ import annotations

import logging

from .cian_url import build_cian_search_url
from .config import ConfigError, Settings
from .db import ListingStore
from .notifier import TelegramNotifier, send_listings_sync
from .scraper import (
    PlaywrightCianScraper,
    RequestsCianScraper,
    ScraperConfig,
    scrape,
)

logger = logging.getLogger(__name__)


def build_scraper(settings: Settings) -> RequestsCianScraper | PlaywrightCianScraper:
    search_url = build_search_url(settings)
    config = ScraperConfig(
        search_url=search_url,
        user_agent=settings.user_agent,
        timeout_seconds=settings.request_timeout_seconds,
        limit=settings.listing_limit,
    )
    if settings.use_playwright:
        return PlaywrightCianScraper(config, headless=settings.playwright_headless)
    return RequestsCianScraper(config)


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


def run_check(settings: Settings) -> int:
    store = ListingStore(settings.database_path)
    store.init()
    effective_settings = settings.with_runtime_overrides(store.get_runtime_settings())

    scraper = build_scraper(effective_settings)
    listings = scrape(scraper, effective_settings.listing_limit)
    saved_count = store.upsert_many(listings)
    logger.info("Fetched %s listings, saved %s records", len(listings), saved_count)

    unsent = store.unsent(effective_settings.listing_limit)
    if not unsent:
        logger.info("No new listings to notify")
        return 0

    if effective_settings.dry_run:
        for listing in unsent:
            logger.info("DRY_RUN listing:\n%s", listing.format_message())
        return len(unsent)

    effective_settings.require_telegram()
    notifier = TelegramNotifier(
        token=effective_settings.telegram_bot_token or "",
        chat_id=effective_settings.telegram_chat_id or "",
    )
    sent_ids = send_listings_sync(notifier, unsent)
    for cian_id in sent_ids:
        store.mark_sent(cian_id)

    return len(sent_ids)
