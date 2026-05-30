from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Listing:
    cian_id: str
    url: str
    title: str | None = None
    price: int | None = None
    address: str | None = None
    rooms: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def short_title(self) -> str:
        if self.title:
            return self.title.strip()
        if self.rooms:
            return f"{self.rooms}, аренда"
        return "Квартира в аренду"

    def format_message(self) -> str:
        lines = [self.short_title()]
        if self.price is not None:
            lines.append(f"Цена: {self.price:,} ₽".replace(",", " "))
        if self.address:
            lines.append(f"Адрес: {self.address}")
        lines.append(self.url)
        return "\n".join(lines)
