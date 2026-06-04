from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

from telegram import Bot
from telegram import InlineKeyboardMarkup
from telegram import ReplyKeyboardMarkup
from telegram import ReplyKeyboardRemove
from telegram.error import NetworkError, RetryAfter, TimedOut

from .models import Listing

logger = logging.getLogger(__name__)
_DEFAULT_RATE_LIMIT_SECONDS = 0.4
_DEFAULT_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_BACKOFF_SECONDS = 2.0


class TelegramRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_sent_at = 0.0

    async def wait(self, min_interval_seconds: float) -> None:
        if min_interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_seconds = max(self._last_sent_at + min_interval_seconds - now, 0)
            self._last_sent_at = now + wait_seconds
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)


@dataclass(frozen=True, slots=True)
class TelegramSendPolicy:
    rate_limit_seconds: float = _DEFAULT_RATE_LIMIT_SECONDS
    retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS
    retry_backoff_seconds: float = _DEFAULT_RETRY_BACKOFF_SECONDS


_GLOBAL_RATE_LIMITER = TelegramRateLimiter()


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        send_policy: TelegramSendPolicy | None = None,
        rate_limiter: TelegramRateLimiter | None = None,
    ) -> None:
        self.bot = Bot(token=token)
        self.chat_id = chat_id
        self.send_policy = send_policy or TelegramSendPolicy()
        self.rate_limiter = rate_limiter or _GLOBAL_RATE_LIMITER

    async def send_listing(self, listing: Listing) -> None:
        await self.send_message(listing.format_message(), disable_web_page_preview=False)

    async def send_message(
        self,
        text: str,
        *,
        disable_web_page_preview: bool = True,
        reply_markup: InlineKeyboardMarkup
        | ReplyKeyboardMarkup
        | ReplyKeyboardRemove
        | None = None,
    ) -> None:
        attempts = max(self.send_policy.retry_attempts, 1)
        for attempt in range(1, attempts + 1):
            await self.rate_limiter.wait(self.send_policy.rate_limit_seconds)
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    disable_web_page_preview=disable_web_page_preview,
                    reply_markup=reply_markup,
                )
                return
            except RetryAfter as exc:
                if attempt >= attempts:
                    raise
                wait_seconds = max(float(exc.retry_after), 0)
                logger.warning(
                    "Telegram rate limited chat_id=%s, retrying attempt %s/%s after %.1f sec",
                    self.chat_id,
                    attempt + 1,
                    attempts,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
            except (NetworkError, TimedOut) as exc:
                if attempt >= attempts:
                    raise
                wait_seconds = max(self.send_policy.retry_backoff_seconds * attempt, 0)
                logger.warning(
                    "Telegram send failed chat_id=%s, retrying attempt %s/%s after %.1f sec: %s",
                    self.chat_id,
                    attempt + 1,
                    attempts,
                    wait_seconds,
                    exc,
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

    async def send_listings(self, listings: list[Listing]) -> list[str]:
        sent_ids: list[str] = []
        for listing in _chat_order(listings):
            await self.send_listing(listing)
            sent_ids.append(listing.cian_id)
            logger.info("Sent listing %s", listing.cian_id)
        return sent_ids


def _chat_order(listings: list[Listing]) -> list[Listing]:
    return list(reversed(listings))


def send_listings_sync(notifier: TelegramNotifier, listings: list[Listing]) -> list[str]:
    return asyncio.run(notifier.send_listings(listings))


def send_message_sync(
    notifier: TelegramNotifier,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
) -> None:
    asyncio.run(notifier.send_message(text, reply_markup=reply_markup))
