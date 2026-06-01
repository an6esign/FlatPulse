from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace

from apscheduler.schedulers.background import BackgroundScheduler

from .bot import build_settings_bot
from .config import ConfigError, Settings
from .db import ListingStore
from .service import build_search_url, classify_check_error, fetch_listings, run_check


def configure_logging(verbose: bool = False, log_level: str | None = None) -> None:
    level_name = "DEBUG" if verbose else (log_level or os.getenv("LOG_LEVEL", "INFO"))
    level = _parse_log_level(level_name)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for logger_name in (
        "httpx",
        "telegram",
        "telegram.ext",
        "apscheduler.executors.default",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def _parse_log_level(value: str) -> int:
    level = logging.getLevelName(value.strip().upper())
    if isinstance(level, int):
        return level
    raise ConfigError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch CIAN rent listings and notify Telegram.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.add_argument("--bot-only", action="store_true", help="Run Telegram bot polling only.")
    parser.add_argument("--worker-only", action="store_true", help="Run scheduled checks only.")
    parser.add_argument("--init-db", action="store_true", help="Create SQLite schema and exit.")
    parser.add_argument("--parser-smoke", action="store_true", help="Check parser and exit.")
    parser.add_argument("--healthcheck", action="store_true", help="Check DB health and exit.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)
    exclusive_modes = [
        args.once,
        args.bot_only,
        args.worker_only,
        args.init_db,
        args.parser_smoke,
        args.healthcheck,
    ]
    if sum(bool(mode) for mode in exclusive_modes) > 1:
        parser.error(
            "--once, --bot-only, --worker-only, --init-db, --parser-smoke "
            "and --healthcheck are mutually exclusive"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    try:
        settings = Settings.from_env()
        settings.validate_environment()
        if args.init_db:
            ListingStore(settings.database_path, settings.database_url).init()
            logging.info("Database initialized")
            return 0

        if args.parser_smoke:
            return run_parser_smoke(settings)

        if args.healthcheck:
            return run_healthcheck(settings)

        if args.once:
            count = run_check(settings)
            logging.info("Done, notified %s listings", count)
            return 0

        if args.bot_only:
            return run_bot(settings)

        if args.worker_only:
            return run_worker(settings)

        scheduler = start_scheduler(settings)
        return run_bot(settings, scheduler=scheduler)
    except (ConfigError, RuntimeError) as exc:
        logging.error("%s", exc)
        return 2


def run_worker(settings: Settings) -> int:
    scheduler = start_scheduler(settings)
    try:
        while True:
            asyncio.run(asyncio.sleep(3600))
    finally:
        scheduler.shutdown(wait=False)


def run_parser_smoke(settings: Settings) -> int:
    smoke_settings = replace(settings, listing_limit=min(settings.listing_limit, 3))
    try:
        url = build_search_url(smoke_settings)
        listings = fetch_listings(smoke_settings)
    except Exception as exc:
        error_type = classify_check_error(exc)
        logging.error("Parser smoke failed: status=%s error=%s", error_type, exc)
        return 2

    logging.info(
        "Parser smoke ok: status=ok listings=%s url=%s",
        len(listings),
        url,
    )
    return 0


def run_healthcheck(settings: Settings) -> int:
    try:
        store = ListingStore(settings.database_path, settings.database_url)
        store.ping()
        schema_version = store.schema_version()
        if schema_version is None:
            logging.error("Healthcheck failed: schema version is unavailable")
            return 2
    except Exception as exc:
        logging.error("Healthcheck failed: %s", exc)
        return 2

    logging.info("Healthcheck ok: db=ok schema=%s", schema_version)
    return 0


def run_bot(settings: Settings, scheduler: BackgroundScheduler | None = None) -> int:
    application = build_settings_bot(settings).build_application()
    logging.info("Starting Telegram bot polling")
    try:
        _ensure_event_loop()
        application.run_polling(close_loop=False)
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
    return 0


def start_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        run_check,
        "interval",
        seconds=settings.check_interval_seconds,
        args=[settings],
        id="cian_check",
        max_instances=1,
        coalesce=True,
    )

    logging.info("Starting scheduler, interval=%ss", settings.check_interval_seconds)
    run_check(settings)
    scheduler.start()
    return scheduler


def _ensure_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


if __name__ == "__main__":
    sys.exit(main())
