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

    def add_leads(self, leads: list[Lead]) -> None: ...

    def update_leads(self, updates: dict[str, dict[str, str]]) -> int:
        """Apply {lead_id: {field: value}}. Returns number of leads updated."""
        ...

    def delete_leads(self, lead_ids: list[str]) -> int: ...

    def load_suppression(self) -> set[str]: ...

    def add_suppression(self, entries: list[SuppressionEntry]) -> None: ...

    def add_activity(self, lead_id: str, type_: str, details: str = "", campaign: str = "") -> None: ...

    def delete_activities(self, lead_ids: list[str]) -> int: ...

    def log_run(self, stats: RunStats) -> None: ...

    def list_runs(self, limit: int = 20) -> list[dict[str, str]]: ...
