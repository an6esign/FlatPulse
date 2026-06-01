import asyncio

from cian_rent_alerts.models import Listing
from cian_rent_alerts.notifier import TelegramNotifier


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
