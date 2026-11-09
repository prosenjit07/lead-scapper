"""SQLite lead store — same interface as Google Sheets; for offline use, tests, or large volumes."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from leadgen.models import (
    ACTIVITY_COLUMNS,
    LEAD_COLUMNS,
    RUN_COLUMNS,
    SUPPRESSION_COLUMNS,
    Lead,
    RunStats,
    SuppressionEntry,
    utcnow_iso,
)


def _cols(cols: list[str]) -> str:
    return ", ".join(f'"{c}" TEXT' for c in cols)


class SQLiteStore:
    name = "sqlite"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.setup()

    def close(self) -> None:
        self._conn.close()

    def setup(self) -> None:
        lead_cols = ", ".join(
            f'"{c}" TEXT' + (" PRIMARY KEY" if c == "lead_id" else "") for c in LEAD_COLUMNS
        )
        with self._conn:
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS leads ({lead_cols})")
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS activities ({_cols(ACTIVITY_COLUMNS)})")
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS suppression ({_cols(SUPPRESSION_COLUMNS)})")
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS runs ({_cols(RUN_COLUMNS)})")
            # Add columns introduced by newer versions without losing data.
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(leads)")}
            for c in LEAD_COLUMNS:
                if c not in existing:
                    self._conn.execute(f'ALTER TABLE leads ADD COLUMN "{c}" TEXT DEFAULT ""')
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_suppression_hash ON suppression(hash)")

    def load_leads(self) -> list[Lead]:
        with closing(self._conn.execute("SELECT * FROM leads ORDER BY created_at")) as cur:
            return [Lead.from_row(dict(r)) for r in cur.fetchall()]

    def add_leads(self, leads: list[Lead]) -> None:
        if not leads:
            return
        placeholders = ", ".join("?" for _ in LEAD_COLUMNS)
        names = ", ".join(f'"{c}"' for c in LEAD_COLUMNS)
        with self._conn:
            self._conn.executemany(
                f"INSERT OR IGNORE INTO leads ({names}) VALUES ({placeholders})",
                [[lead.to_row()[c] for c in LEAD_COLUMNS] for lead in leads],
            )

    def update_leads(self, updates: dict[str, dict[str, str]]) -> int:
        count = 0
        with self._conn:
            for lead_id, fields in updates.items():
                fields = {k: v for k, v in fields.items() if k in LEAD_COLUMNS and k != "lead_id"}
                if not fields:
                    continue
                fields.setdefault("updated_at", utcnow_iso())
                sets = ", ".join(f'"{k}" = ?' for k in fields)
                cur = self._conn.execute(
                    f"UPDATE leads SET {sets} WHERE lead_id = ?", [*fields.values(), lead_id]
                )
                count += cur.rowcount
        return count

    def delete_leads(self, lead_ids: list[str]) -> int:
        with self._conn:
            return sum(
                self._conn.execute("DELETE FROM leads WHERE lead_id = ?", (i,)).rowcount for i in lead_ids
            )

    def load_suppression(self) -> set[str]:
        return {r["hash"] for r in self._conn.execute("SELECT hash FROM suppression")}

    def add_suppression(self, entries: list[SuppressionEntry]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO suppression (hash, kind, reason, added_at) VALUES (?, ?, ?, ?)",
                [(e.hash, e.kind, e.reason, e.added_at) for e in entries],
            )

    def add_activity(self, lead_id: str, type_: str, details: str = "", campaign: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO activities (lead_id, timestamp, type, details, campaign) VALUES (?, ?, ?, ?, ?)",
                (lead_id, utcnow_iso(), type_, details, campaign),
            )

    def delete_activities(self, lead_ids: list[str]) -> int:
        with self._conn:
            return sum(
                self._conn.execute("DELETE FROM activities WHERE lead_id = ?", (i,)).rowcount
                for i in lead_ids
            )

    def log_run(self, stats: RunStats) -> None:
        row = stats.to_row()
        with self._conn:
            self._conn.execute(
                f"INSERT INTO runs ({', '.join(RUN_COLUMNS)}) VALUES ({', '.join('?' for _ in RUN_COLUMNS)})",
                [row.get(c, "") for c in RUN_COLUMNS],
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, str]]:
        cur = self._conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def list_activities(self, lead_id: str | None = None) -> list[dict[str, str]]:
        if lead_id:
            cur = self._conn.execute(
                "SELECT * FROM activities WHERE lead_id = ? ORDER BY timestamp", (lead_id,)
            )
        else:
            cur = self._conn.execute("SELECT * FROM activities ORDER BY timestamp")
        return [dict(r) for r in cur.fetchall()]
