from __future__ import annotations


def payment_success_text(paid_until_label: str | None) -> str:
    return "\n".join(
        [
            "✅ Оплата прошла.",
            "",
            f"Подписка активна до {paid_until_label or '-'}.",
            "Уведомления включены. Новые квартиры будут приходить автоматически.",
        ]
    )
