"""Operational crawl state (not leads): when each domain was last crawled, and cached geocodes.
Keeps scheduled runs from re-crawling the same sites and re-hitting Nominatim."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class CrawlState:
    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS crawled (domain TEXT PRIMARY KEY, last_crawled_at TEXT, outcome TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS geocode (query TEXT PRIMARY KEY, result TEXT, at TEXT)"
            )

    def close(self) -> None:
        self._conn.close()

    def recently_crawled(self, domain: str, within_days: int) -> bool:
        if within_days <= 0:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT last_crawled_at FROM crawled WHERE domain = ?", (domain,)
            ).fetchone()
        if not row:
            return False
        try:
            last = datetime.fromisoformat(row[0])
        except ValueError:
            return False
        return datetime.now(UTC) - last < timedelta(days=within_days)

    def mark_crawled(self, domain: str, outcome: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO crawled (domain, last_crawled_at, outcome) VALUES (?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET last_crawled_at = excluded.last_crawled_at, outcome = excluded.outcome",
                (domain, datetime.now(UTC).isoformat(), outcome),
            )

    def forget_domain(self, domain: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM crawled WHERE domain = ?", (domain,))

    def get_geocode(self, query: str, max_age_days: int = 90) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result, at FROM geocode WHERE query = ?", (query.lower(),)
            ).fetchone()
        if not row:
            return None
        if datetime.now(UTC) - datetime.fromisoformat(row[1]) > timedelta(days=max_age_days):
            return None
        return json.loads(row[0])

    def put_geocode(self, query: str, result: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO geocode (query, result, at) VALUES (?, ?, ?)",
                (query.lower(), json.dumps(result), datetime.now(UTC).isoformat()),
            )
