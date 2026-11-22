"""Storage interface. Implementations: Google Sheets (default, shared with humans) and SQLite (local).
A future Notion / HubSpot / Postgres adapter only needs to implement this protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from leadgen.models import Lead, RunStats, SuppressionEntry


@runtime_checkable
class LeadStore(Protocol):
    name: str

    def setup(self) -> None:
        """Create tabs/tables, headers, validation rules. Safe to run repeatedly."""

    def load_leads(self) -> list[Lead]: ...

    def add_leads(self, leads: list[Lead]) -> int:
        """Insert new leads; leads whose lead_id already exists are skipped (PostgreSQL also skips a
        domain or email that another lead already has). Returns the number actually inserted."""
        ...

    def update_leads(self, updates: dict[str, dict[str, str]]) -> int:
        """Apply {lead_id: {field: value}}. Returns number of leads updated."""
        ...

    def delete_leads(self, lead_ids: list[str]) -> int: ...

    def load_suppression(self) -> set[str]: ...

    def list_suppression(self) -> list[SuppressionEntry]:
        """Full suppression entries (hash, kind, reason, added_at); used to copy data between stores."""
        ...

    def add_suppression(self, entries: list[SuppressionEntry]) -> None: ...

    def add_activity(self, lead_id: str, type_: str, details: str = "", campaign: str = "") -> None: ...

    def list_activities(self, lead_id: str | None = None) -> list[dict[str, str]]:
        """Activities (oldest first), optionally for one lead."""
        ...

    def delete_activities(self, lead_ids: list[str]) -> int: ...

    def log_run(self, stats: RunStats) -> None: ...

    def list_runs(self, limit: int = 20) -> list[dict[str, str]]: ...
