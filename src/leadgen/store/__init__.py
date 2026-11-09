"""Lead storage backends. Use `open_store()` to get the configured one."""

from __future__ import annotations

from leadgen.config import AppConfig, ConfigError, Secrets
from leadgen.store.base import LeadStore


def open_store(cfg: AppConfig, secrets: Secrets, kind: str | None = None) -> LeadStore:
    kind = kind or cfg.store
    if kind == "sqlite":
        from leadgen.store.sqlite_store import SQLiteStore

        return SQLiteStore(cfg.sqlite_path)
    if kind == "sheets":
        from leadgen.store.sheets import SheetsStore

        if not secrets.google_sheet_id or not secrets.google_service_account_file:
            raise ConfigError(
                "Google Sheets store needs GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE in .env "
                "(or use --store sqlite for local testing)"
            )
        if not secrets.google_service_account_file.exists():
            raise ConfigError(f"service account file not found: {secrets.google_service_account_file}")
        return SheetsStore.connect(secrets.google_sheet_id, secrets.google_service_account_file)
    raise ConfigError(f"unknown store '{kind}'")


__all__ = ["LeadStore", "open_store"]
