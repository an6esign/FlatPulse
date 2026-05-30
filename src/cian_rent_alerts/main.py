from __future__ import annotations

import argparse
import logging
import sys

from apscheduler.schedulers.background import BackgroundScheduler

from .bot import build_settings_bot
from .config import ConfigError, Settings
from .db import ListingStore
from .service import run_check


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch CIAN rent listings and notify Telegram.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.add_argument("--init-db", action="store_true", help="Create SQLite schema and exit.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    try:
        settings = Settings.from_env()
        if args.init_db:
            ListingStore(settings.database_path).init()
            logging.info("Database initialized at %s", settings.database_path)
            return 0

        if args.once:
            count = run_check(settings)
            logging.info("Done, notified %s listings", count)
            return 0

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
        application = build_settings_bot(settings).build_application()
        logging.info("Starting Telegram bot polling")
        try:
            application.run_polling(close_loop=False)
        finally:
            scheduler.shutdown(wait=False)
        return 0
    except (ConfigError, RuntimeError) as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
