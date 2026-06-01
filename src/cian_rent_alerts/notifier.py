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
        await self.send_message(listing.format_message(), disable_web_page_preview=False)

    async def send_message(
        self,
        text: str,
        *,
        disable_web_page_preview: bool = True,
    ) -> None:
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            disable_web_page_preview=disable_web_page_preview,
        )

    async def send_listings(self, listings: list[Listing]) -> list[str]:
        sent_ids: list[str] = []
        for listing in _chat_order(listings):
            await self.send_listing(listing)
            sent_ids.append(listing.cian_id)
            logger.info("Sent listing %s", listing.cian_id)
            await asyncio.sleep(0.4)
        return sent_ids


def _chat_order(listings: list[Listing]) -> list[Listing]:
    return list(reversed(listings))


def send_listings_sync(notifier: TelegramNotifier, listings: list[Listing]) -> list[str]:
    return asyncio.run(notifier.send_listings(listings))


def send_message_sync(notifier: TelegramNotifier, text: str) -> None:
    asyncio.run(notifier.send_message(text))
