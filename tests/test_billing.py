from dataclasses import replace

import pytest

from cian_rent_alerts.billing import YooKassaClient, billing_is_configured
from cian_rent_alerts.config import ConfigError, Settings


def test_billing_is_configured_requires_all_yookassa_settings() -> None:
    settings = Settings.from_env(env_file=None)

    assert billing_is_configured(settings) is False
    assert (
        billing_is_configured(
            replace(
                settings,
                yookassa_shop_id="shop",
                yookassa_secret_key="secret",
                yookassa_return_url="https://example.com/return",
            )
        )
        is True
    )


def test_yookassa_client_creates_redirect_payment(monkeypatch) -> None:
    settings = replace(
        Settings.from_env(env_file=None),
        yookassa_shop_id="shop-id",
        yookassa_secret_key="secret-key",
        yookassa_return_url="https://example.com/return",
    )
    requests_seen: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "id": "payment-1",
                "status": "pending",
                "paid": False,
                "confirmation": {"confirmation_url": "https://yookassa.test/pay"},
            }

    def fake_request(method, url, **kwargs):
        requests_seen.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr("cian_rent_alerts.billing.requests.request", fake_request)

    payment = YooKassaClient(settings).create_payment(
        amount_rub=199,
        description="FlatPulse subscription",
        user_id=42,
    )

    assert payment.provider_payment_id == "payment-1"
    assert payment.status == "pending"
    assert payment.confirmation_url == "https://yookassa.test/pay"
    request = requests_seen[0]
    assert request["method"] == "POST"
    assert request["auth"] == ("shop-id", "secret-key")
    assert request["json"]["amount"] == {"value": "199.00", "currency": "RUB"}
    assert request["json"]["confirmation"]["type"] == "redirect"
    assert request["json"]["confirmation"]["return_url"] == "https://example.com/return"
    assert "secret-key" not in str(request["json"])


def test_yookassa_client_requires_credentials() -> None:
    with pytest.raises(ConfigError):
        YooKassaClient(Settings.from_env(env_file=None))
