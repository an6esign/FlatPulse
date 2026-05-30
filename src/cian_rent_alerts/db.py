from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .models import Listing


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ListingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    cian_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT,
                    price INTEGER,
                    address TEXT,
                    rooms TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    sent_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_sent_at ON listings(sent_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def upsert_many(self, listings: Iterable[Listing]) -> int:
        now = utc_now()
        rows = list(listings)
        with self.connect() as conn:
            for listing in rows:
                conn.execute(
                    """
                    INSERT INTO listings (
                        cian_id, url, title, price, address, rooms, raw_json,
                        first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cian_id) DO UPDATE SET
                        url = excluded.url,
                        title = excluded.title,
                        price = excluded.price,
                        address = excluded.address,
                        rooms = excluded.rooms,
                        raw_json = excluded.raw_json,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        listing.cian_id,
                        listing.url,
                        listing.title,
                        listing.price,
                        listing.address,
                        listing.rooms,
                        json.dumps(listing.raw, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
        return len(rows)

    def unsent(self, limit: int) -> list[Listing]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT cian_id, url, title, price, address, rooms, raw_json
                FROM listings
                WHERE sent_at IS NULL
                ORDER BY first_seen_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [self._row_to_listing(row) for row in rows]

    def mark_sent(self, cian_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE listings SET sent_at = ? WHERE cian_id = ?",
                (utc_now(), cian_id),
            )

    def mark_all_unsent_as_sent(self) -> int:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE listings SET sent_at = ? WHERE sent_at IS NULL",
                (now,),
            )
            return cursor.rowcount

    def get_runtime_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_runtime_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def delete_runtime_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))

    def clear_runtime_settings(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM app_settings")

    @staticmethod
    def _row_to_listing(row: sqlite3.Row) -> Listing:
        try:
            raw = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            raw = {}
        return Listing(
            cian_id=row["cian_id"],
            url=row["url"],
            title=row["title"],
            price=row["price"],
            address=row["address"],
            rooms=row["rooms"],
            raw=raw,
        )
