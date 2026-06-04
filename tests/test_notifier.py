import asyncio

from telegram.error import NetworkError, RetryAfter

from cian_rent_alerts.models import Listing
from cian_rent_alerts.notifier import TelegramNotifier, TelegramRateLimiter, TelegramSendPolicy


def test_send_listings_posts_oldest_first(monkeypatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    sent_ids: list[str] = []

    async def fake_send_listing(listing: Listing) -> None:
        sent_ids.append(listing.cian_id)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    notifier.send_listing = fake_send_listing

    result = asyncio.run(
        notifier.send_listings(
            [
                Listing(cian_id="new", url="https://example.com/new"),
                Listing(cian_id="middle", url="https://example.com/middle"),
                Listing(cian_id="old", url="https://example.com/old"),
            ]
        )
    )

    assert sent_ids == ["old", "middle", "new"]
    assert result == ["old", "middle", "new"]


def test_send_message_retries_after_telegram_retry_after(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    class FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **_kwargs: object) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RetryAfter(2)

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.bot = FakeBot()
    notifier.chat_id = "100"
    notifier.send_policy = TelegramSendPolicy(
        rate_limit_seconds=0,
        retry_attempts=2,
        retry_backoff_seconds=0,
    )
    notifier.rate_limiter = TelegramRateLimiter()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(notifier.send_message("test"))

    assert notifier.bot.calls == 2
    assert sleeps == [2.0]


def test_send_message_retries_network_errors_with_backoff(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    class FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **_kwargs: object) -> None:
            self.calls += 1
            if self.calls == 1:
                raise NetworkError("temporary telegram error")

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.bot = FakeBot()
    notifier.chat_id = "100"
    notifier.send_policy = TelegramSendPolicy(
        rate_limit_seconds=0,
        retry_attempts=2,
        retry_backoff_seconds=3,
    )
    notifier.rate_limiter = TelegramRateLimiter()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(notifier.send_message("test"))

    assert notifier.bot.calls == 2
    assert sleeps == [3]


def test_send_message_waits_between_messages(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    class FakeBot:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, **_kwargs: object) -> None:
            self.calls += 1

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.bot = FakeBot()
    notifier.chat_id = "100"
    notifier.send_policy = TelegramSendPolicy(
        rate_limit_seconds=1,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )
    notifier.rate_limiter = TelegramRateLimiter()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    asyncio.run(notifier.send_message("first"))
    asyncio.run(notifier.send_message("second"))

    assert notifier.bot.calls == 2
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 1
