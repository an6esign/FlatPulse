from __future__ import annotations

import asyncio
import logging

from telegram import Bot

from .models import Listing

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.bot = Bot(token=token)
        self.chat_id = chat_id

    async def send_listing(self, listing: Listing) -> None:
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=listing.format_message(),
            disable_web_page_preview=False,
        )

    async def send_listings(self, listings: list[Listing]) -> list[str]:
        sent_ids: list[str] = []
        for listing in listings:
            await self.send_listing(listing)
            sent_ids.append(listing.cian_id)
            logger.info("Sent listing %s", listing.cian_id)
            await asyncio.sleep(0.4)
        return sent_ids


def send_listings_sync(notifier: TelegramNotifier, listings: list[Listing]) -> list[str]:
    return asyncio.run(notifier.send_listings(listings))
