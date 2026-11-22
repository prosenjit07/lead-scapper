"""Lead storage backends. Use `open_store()` to get the configured one."""

from __future__ import annotations

from leadgen.config import AppConfig, ConfigError, Secrets, install_hint
from leadgen.store.base import LeadStore

STORE_KINDS = ("sheets", "sqlite", "postgres")


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
    if kind == "postgres":
        if not secrets.database_url:
            raise ConfigError(
                "PostgreSQL store needs DATABASE_URL in .env, e.g. postgresql://user:password@localhost:5432/leadgen"
            )
        try:
            from leadgen.store.postgres_store import PostgresStore
        except ImportError as e:
            raise ConfigError(f"PostgreSQL store needs: {install_hint('postgres')}") from e
        return PostgresStore.connect(secrets.database_url)
    raise ConfigError(f"unknown store '{kind}' (expected one of: {', '.join(STORE_KINDS)})")


__all__ = ["STORE_KINDS", "LeadStore", "open_store"]
