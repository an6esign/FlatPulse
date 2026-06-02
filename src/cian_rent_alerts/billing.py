from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import requests

from .config import ConfigError, Settings

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"


@dataclass(frozen=True, slots=True)
class PaymentInit:
    provider_payment_id: str
    status: str
    confirmation_url: str | None
    raw_json: str


@dataclass(frozen=True, slots=True)
class PaymentStatus:
    provider_payment_id: str
    status: str
    paid: bool
    raw_json: str


class YooKassaClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.yookassa_shop_id:
            raise ConfigError("YOOKASSA_SHOP_ID is required to create payments")
        if not settings.yookassa_secret_key:
            raise ConfigError("YOOKASSA_SECRET_KEY is required to create payments")
        if not settings.yookassa_return_url:
            raise ConfigError("YOOKASSA_RETURN_URL is required to create payments")

        self.shop_id = settings.yookassa_shop_id
        self.secret_key = settings.yookassa_secret_key
        self.return_url = settings.yookassa_return_url

    def create_payment(
        self,
        *,
        amount_rub: int,
        description: str,
        user_id: int,
    ) -> PaymentInit:
        payload = {
            "amount": {
                "value": f"{amount_rub}.00",
                "currency": "RUB",
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": self.return_url,
            },
            "description": description,
            "metadata": {
                "user_id": str(user_id),
                "product": "flatpulse_subscription",
            },
        }
        data = self._request("POST", "/payments", json_body=payload)
        return PaymentInit(
            provider_payment_id=str(data["id"]),
            status=str(data["status"]),
            confirmation_url=_confirmation_url(data),
            raw_json=json.dumps(data, ensure_ascii=False),
        )

    def get_payment(self, provider_payment_id: str) -> PaymentStatus:
        data = self._request("GET", f"/payments/{provider_payment_id}")
        return PaymentStatus(
            provider_payment_id=str(data["id"]),
            status=str(data["status"]),
            paid=bool(data.get("paid")),
            raw_json=json.dumps(data, ensure_ascii=False),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotence-Key": str(uuid.uuid4())} if method == "POST" else None
        response = requests.request(
            method,
            f"{YOOKASSA_API_BASE}{path}",
            auth=(self.shop_id, self.secret_key),
            headers=headers,
            json=json_body,
            timeout=20,
        )
        if response.status_code >= 400:
            raise ConfigError(f"YooKassa request failed: status={response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise ConfigError("YooKassa returned an unexpected response")
        return data


def billing_is_configured(settings: Settings) -> bool:
    return bool(
        settings.yookassa_shop_id and settings.yookassa_secret_key and settings.yookassa_return_url
    )


def _confirmation_url(data: dict[str, Any]) -> str | None:
    confirmation = data.get("confirmation")
    if not isinstance(confirmation, dict):
        return None
    value = confirmation.get("confirmation_url")
    return str(value) if value else None
