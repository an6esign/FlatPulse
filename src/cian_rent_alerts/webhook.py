from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .analytics import EV_PAYMENT_SUCCEEDED, EV_WEBHOOK_ERROR
from .config import ConfigError, Settings
from .db import ListingStore
from .notifier import TelegramNotifier, send_message_sync

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookResult:
    status: str
    provider_payment_id: str | None = None
    user_id: int | None = None
    paid_until: str | None = None


def run_webhook_server(settings: Settings) -> int:
    if not settings.yookassa_webhook_secret:
        raise ConfigError("YOOKASSA_WEBHOOK_SECRET is required to run webhook server")

    store = ListingStore(settings.database_path, settings.database_url)
    store.init()

    handler = _build_handler(settings, store)
    server = ThreadingHTTPServer((settings.webhook_host, settings.webhook_port), handler)
    logger.info(
        "Starting webhook server host=%s port=%s",
        settings.webhook_host,
        settings.webhook_port,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def process_yookassa_webhook(
    store: ListingStore,
    settings: Settings,
    payload: dict[str, Any],
    *,
    notify_user: bool = True,
) -> WebhookResult:
    event = str(payload.get("event") or "")
    obj = payload.get("object")
    if not isinstance(obj, dict):
        return WebhookResult(status="ignored")

    provider_payment_id = str(obj.get("id") or "")
    if not provider_payment_id:
        return WebhookResult(status="ignored")

    payment = store.payment_by_provider_payment_id(provider_payment_id)
    if payment is None:
        logger.warning("Ignored YooKassa webhook for unknown payment_id=%s", provider_payment_id)
        _record_webhook_error(store, "unknown_payment")
        return WebhookResult(status="unknown_payment", provider_payment_id=provider_payment_id)

    user_id = int(payment["user_id"])
    payment_status = str(obj.get("status") or payment.get("status") or "")
    raw_json = json.dumps(obj, ensure_ascii=False)

    if event != "payment.succeeded" or payment_status != "succeeded" or not obj.get("paid"):
        store.update_payment(
            provider_payment_id, status=payment_status or "unknown", raw_json=raw_json
        )
        return WebhookResult(
            status="updated",
            provider_payment_id=provider_payment_id,
            user_id=user_id,
        )

    existing_paid_until = payment.get("paid_until")
    if existing_paid_until:
        store.update_payment(
            provider_payment_id,
            status="succeeded",
            paid_until=str(existing_paid_until),
            raw_json=raw_json,
        )
        return WebhookResult(
            status="already_processed",
            provider_payment_id=provider_payment_id,
            user_id=user_id,
            paid_until=str(existing_paid_until),
        )

    paid_until = store.grant_paid_access(user_id, days=settings.subscription_period_days)
    store.update_payment(
        provider_payment_id,
        status="succeeded",
        paid_until=paid_until,
        raw_json=raw_json,
    )
    search = store.current_search_for_user(user_id)
    if search is not None:
        store.update_search(int(search["id"]), is_active=True)
        search_id = int(search["id"])
    else:
        search_id = None
    store.record_event(EV_PAYMENT_SUCCEEDED, user_id=user_id, search_id=search_id)

    if notify_user and not settings.dry_run and settings.telegram_bot_token:
        _notify_user_about_payment(store, settings, user_id, paid_until)

    return WebhookResult(
        status="processed",
        provider_payment_id=provider_payment_id,
        user_id=user_id,
        paid_until=paid_until,
    )


def _notify_user_about_payment(
    store: ListingStore,
    settings: Settings,
    user_id: int,
    paid_until: str | None,
) -> None:
    user = store.get_user(user_id)
    if user is None:
        return
    chat_id = str(user["telegram_chat_id"])
    text = "\n".join(
        [
            "✅ Оплата прошла.",
            "",
            f"Подписка активна до {paid_until or '-'}.",
            "Новые квартиры будут приходить автоматически.",
        ]
    )
    try:
        notifier = TelegramNotifier(settings.telegram_bot_token or "", chat_id=chat_id)
        send_message_sync(notifier, text)
    except Exception:
        logger.exception("Failed to send YooKassa payment confirmation to user_id=%s", user_id)


def _build_handler(settings: Settings, store: ListingStore) -> type[BaseHTTPRequestHandler]:
    class YooKassaWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            expected_path = f"/webhooks/yookassa/{settings.yookassa_webhook_secret}"
            if parsed.path != expected_path:
                self._write_json(404, {"status": "not_found"})
                return

            try:
                body = self.rfile.read(_content_length(self))
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
            except Exception:
                _record_webhook_error(store, "bad_request")
                self._write_json(400, {"status": "bad_request"})
                return

            try:
                result = process_yookassa_webhook(store, settings, payload)
            except Exception:
                logger.exception("Failed to process YooKassa webhook")
                _record_webhook_error(store, "process_error")
                self._write_json(500, {"status": "error"})
                return

            self._write_json(200, {"status": result.status})

        def log_message(self, format: str, *args: object) -> None:
            safe_args = tuple(
                _redact_webhook_secret(str(arg), settings.yookassa_webhook_secret)
                if isinstance(arg, str)
                else arg
                for arg in args
            )
            logger.info("Webhook request: " + format, *safe_args)

        def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return YooKassaWebhookHandler


def _content_length(handler: BaseHTTPRequestHandler) -> int:
    try:
        return max(int(handler.headers.get("Content-Length", "0")), 0)
    except ValueError:
        return 0


def _redact_webhook_secret(value: str, secret: str | None) -> str:
    if not secret:
        return value
    return value.replace(f"/webhooks/yookassa/{secret}", "/webhooks/yookassa/<secret>")


def _record_webhook_error(store: ListingStore, reason: str) -> None:
    try:
        store.record_event(EV_WEBHOOK_ERROR, metadata={"reason": reason})
    except Exception:
        logger.exception("Failed to record webhook error reason=%s", reason)
