"""PostgreSQL schema migrations (Alembic), driven from the `leads db …` commands.

Migrations live in `store/migrations/versions/` and are plain SQL (`op.execute`), no ORM models.
The database URL is passed in memory (never written to a config file), so `.env` stays the only place
that holds it."""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def sqlalchemy_url(dsn: str) -> str:
    """postgres://… or postgresql://… → postgresql+psycopg://… (the driver Alembic should use)."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+psycopg://" + dsn[len(prefix) :]
    raise ValueError("DATABASE_URL must start with postgresql:// or postgres://")


def _config(dsn: str):  # -> alembic.config.Config
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["dsn"] = dsn  # read by migrations/env.py; avoids configparser '%' interpolation
    return cfg


def head_revision() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_config("postgresql://unused")).get_current_head()


def current_revision(conn) -> str | None:  # conn: psycopg.Connection
    """Revision recorded in the database, or None for an empty (never migrated) database."""
    import psycopg

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            row = cur.fetchone()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None
    if row is None:
        return None
    return row["version_num"] if isinstance(row, dict) else row[0]


def upgrade(dsn: str, revision: str = "head", sql: bool = False) -> None:
    """Apply migrations up to `revision`. With sql=True, print the SQL instead of running it."""
    from alembic import command

    command.upgrade(_config(dsn), revision, sql=sql)


def downgrade(dsn: str, revision: str) -> None:
    from alembic import command

    command.downgrade(_config(dsn), revision)
