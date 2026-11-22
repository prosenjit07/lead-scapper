"""PostgreSQL lead store: the same LeadStore interface as Google Sheets and SQLite, for production use
(several programs writing at once, server deployment). Needs the `postgres` extra and
DATABASE_URL in .env; create/upgrade the schema with `leads db migrate`.

The rest of the code keeps working with string values ("" = unknown); this adapter converts timestamps
to/from `timestamptz` (always returned as ISO 8601 in UTC) and run counters to/from integers."""

from __future__ import annotations

import atexit
import threading
import weakref
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from leadgen.config import ConfigError
from leadgen.logging_setup import get_logger
from leadgen.models import LEAD_COLUMNS, Lead, RunStats, SuppressionEntry, utcnow_iso
from leadgen.store import migrate

log = get_logger("postgres")

LEAD_TIMESTAMPS = {"last_contacted_at", "next_follow_up_at", "created_at", "updated_at"}
LEAD_REQUIRED_TIMESTAMPS = {"created_at", "updated_at"}  # NOT NULL in the schema
UPDATABLE = [c for c in LEAD_COLUMNS if c != "lead_id"]
RUN_INT_FIELDS = ("discovered", "crawled", "new", "duplicates", "merged", "suppressed", "skipped", "errors")


# ---------------------------------------------------------------------------- value conversion
def to_ts(value: Any, field: str = "timestamp") -> datetime | None:
    """'' / None → NULL; ISO 8601 text → aware datetime (values without a timezone are taken as UTC).
    Raises ValueError for anything else, so bad data fails loudly instead of being dropped."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except ValueError as e:
            raise ValueError(f"{field}: {value!r} is not an ISO 8601 date or timestamp") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def from_ts(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat() if value is not None else ""


def _lead_params(lead: Lead) -> dict[str, Any]:
    row = lead.to_row()
    params: dict[str, Any] = {}
    for col in LEAD_COLUMNS:
        if col in LEAD_TIMESTAMPS:
            ts = to_ts(row[col], col)
            params[col] = ts if ts or col not in LEAD_REQUIRED_TIMESTAMPS else datetime.now(UTC)
        else:
            params[col] = row[col]
    return params


def _lead_from_db(row: dict[str, Any]) -> Lead:
    return Lead.from_row({k: (from_ts(v) if k in LEAD_TIMESTAMPS else v) for k, v in row.items()})


def _strs(row: dict[str, Any]) -> dict[str, str]:
    return {
        k: from_ts(v) if isinstance(v, datetime) else ("" if v is None else str(v)) for k, v in row.items()
    }


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("SET TIME ZONE 'UTC'")
    conn.commit()


_OPEN_STORES: weakref.WeakSet[PostgresStore] = weakref.WeakSet()


@atexit.register
def _close_open_stores() -> None:
    for store in list(_OPEN_STORES):
        store.close()


INSERT_LEAD = sql.SQL("INSERT INTO leads ({cols}) VALUES ({vals}) ON CONFLICT DO NOTHING").format(
    cols=sql.SQL(", ").join(map(sql.Identifier, LEAD_COLUMNS)),
    vals=sql.SQL(", ").join(sql.Placeholder(c) for c in LEAD_COLUMNS),
)


class PostgresStore:
    name = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        max_size: int = 5,
        connect_timeout: float = 10.0,
        application_name: str = "leadgen",  # shows in pg_stat_activity / the Neon console
    ) -> None:
        self.dsn = dsn
        try:  # one direct attempt first: gives libpq's real reason (wrong password, no such database …)
            psycopg.connect(dsn, connect_timeout=int(connect_timeout)).close()
        except psycopg.OperationalError as e:
            raise ConfigError(f"cannot connect to PostgreSQL (check DATABASE_URL): {e}".strip()) from e
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "application_name": application_name},
            configure=_configure,
            # Hosted databases close idle connections (Neon pauses after 5 idle minutes; laptops sleep).
            # Check each connection when it's handed out and replace dead ones, so a long-running
            # dashboard/API doesn't fail with "SSL connection has been closed unexpectedly".
            check=ConnectionPool.check_connection,
            open=False,
            name="leadgen",
        )
        self.pool.open(wait=True, timeout=connect_timeout)
        self._closed = False
        self._close_lock = threading.Lock()
        _OPEN_STORES.add(self)  # closed at interpreter exit if the caller didn't

    @classmethod
    def connect(cls, dsn: str, *, check_schema: bool = True, **kwargs: Any) -> PostgresStore:
        store = cls(dsn, **kwargs)
        if check_schema:
            try:
                store.check_schema()
            except Exception:
                store.close()
                raise
        return store

    def close(self) -> None:
        with self._close_lock:
            if not self._closed:
                self._closed = True
                self.pool.close()

    # ------------------------------------------------------------------ schema
    def setup(self) -> None:
        """Create or upgrade the schema (same as `leads db migrate`). Safe to run repeatedly."""
        migrate.upgrade(self.dsn)

    def schema_revision(self) -> str | None:
        with self.pool.connection() as conn:
            return migrate.current_revision(conn)

    def check_schema(self) -> None:
        current, head = self.schema_revision(), migrate.head_revision()
        if current != head:
            raise ConfigError(
                f"PostgreSQL schema is at {current or 'nothing (empty database)'}, latest is {head}: "
                "run `leads db migrate`"
            )

    def server_version(self) -> str:
        with self.pool.connection() as conn:
            row = conn.execute("SHOW server_version").fetchone()
        return str(row["server_version"]) if row else "?"

    # ------------------------------------------------------------------ leads
    def load_leads(self) -> list[Lead]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM leads ORDER BY created_at, lead_id").fetchall()
        return [_lead_from_db(r) for r in rows]

    def add_leads(self, leads: list[Lead]) -> int:
        """Insert new leads; rows whose lead_id, domain or email already exists are skipped.
        Returns the number actually inserted."""
        if not leads:
            return 0
        params = [_lead_params(lead) for lead in leads]
        inserted = 0
        with self.pool.connection() as conn, conn.cursor() as cur:
            for p in params:
                cur.execute(INSERT_LEAD, p)
                inserted += max(cur.rowcount, 0)
        if inserted < len(leads):
            log.info("skipped leads that already exist", extra={"skipped": len(leads) - inserted})
        return inserted

    def update_leads(self, updates: dict[str, dict[str, str]]) -> int:
        count = 0
        with self.pool.connection() as conn:
            for lead_id, fields in updates.items():
                fields = {k: v for k, v in fields.items() if k in UPDATABLE}
                if not fields:
                    continue
                fields.setdefault("updated_at", utcnow_iso())
                params = {k: (to_ts(v, k) if k in LEAD_TIMESTAMPS else v) for k, v in fields.items()}
                if params.get("updated_at") is None:
                    params["updated_at"] = datetime.now(UTC)
                stmt = sql.SQL("UPDATE leads SET {sets} WHERE lead_id = %(_lead_id)s").format(
                    sets=sql.SQL(", ").join(
                        sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in params
                    )
                )
                try:
                    with conn.transaction():  # savepoint: one conflicting lead doesn't undo the others
                        count += max(conn.execute(stmt, {**params, "_lead_id": lead_id}).rowcount, 0)
                except psycopg.errors.UniqueViolation as e:
                    log.warning(
                        "update skipped: another lead already has this domain/email",
                        extra={"lead_id": lead_id, "error": str(e).splitlines()[0]},
                    )
        return count

    def delete_leads(self, lead_ids: list[str]) -> int:
        if not lead_ids:
            return 0
        with self.pool.connection() as conn:
            return conn.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (list(lead_ids),)).rowcount

    # ------------------------------------------------------------------ suppression
    def load_suppression(self) -> set[str]:
        with self.pool.connection() as conn:
            return {r["hash"] for r in conn.execute("SELECT hash FROM suppression")}

    def list_suppression(self) -> list[SuppressionEntry]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT hash, kind, reason, added_at FROM suppression ORDER BY added_at")
            return [SuppressionEntry(r["hash"], r["kind"], r["reason"], from_ts(r["added_at"])) for r in rows]

    def add_suppression(self, entries: list[SuppressionEntry]) -> None:
        if not entries:
            return
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO suppression (hash, kind, reason, added_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (hash) DO NOTHING",
                [
                    (e.hash, e.kind, e.reason, to_ts(e.added_at, "added_at") or datetime.now(UTC))
                    for e in entries
                ],
            )

    # ------------------------------------------------------------------ activities
    def add_activity(self, lead_id: str, type_: str, details: str = "", campaign: str = "") -> None:
        self.add_activities([{"lead_id": lead_id, "timestamp": utcnow_iso(), "type": type_,
                              "details": details, "campaign": campaign}])  # fmt: skip

    def add_activities(self, rows: list[dict[str, str]]) -> None:
        """Insert activities keeping their original timestamps (used by `leads db import`)."""
        if not rows:
            return
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                'INSERT INTO activities (lead_id, "timestamp", type, details, campaign) VALUES (%s, %s, %s, %s, %s)',
                [(r["lead_id"], to_ts(r.get("timestamp"), "timestamp") or datetime.now(UTC), r.get("type", ""),
                  r.get("details", ""), r.get("campaign", "")) for r in rows],
            )  # fmt: skip

    def list_activities(self, lead_id: str | None = None) -> list[dict[str, str]]:
        query = 'SELECT lead_id, "timestamp", type, details, campaign FROM activities'
        with self.pool.connection() as conn:
            if lead_id:
                rows = conn.execute(query + ' WHERE lead_id = %s ORDER BY "timestamp", id', (lead_id,))
            else:
                rows = conn.execute(query + ' ORDER BY "timestamp", id')
            return [_strs(r) for r in rows]

    def delete_activities(self, lead_ids: list[str]) -> int:
        if not lead_ids:
            return 0
        with self.pool.connection() as conn:
            return conn.execute("DELETE FROM activities WHERE lead_id = ANY(%s)", (list(lead_ids),)).rowcount

    # ------------------------------------------------------------------ runs
    def log_run(self, stats: RunStats) -> None:
        row = stats.to_row()
        cols = ["run_id", "started_at", "finished_at", "search", "sources", *RUN_INT_FIELDS, "notes"]
        params: dict[str, Any] = {c: row.get(c, "") for c in cols}
        params["started_at"] = to_ts(params["started_at"], "started_at") or datetime.now(UTC)
        params["finished_at"] = to_ts(params["finished_at"], "finished_at")
        for f in RUN_INT_FIELDS:
            params[f] = int(params[f] or 0)
        stmt = sql.SQL("INSERT INTO runs ({cols}) VALUES ({vals}) ON CONFLICT (run_id) DO NOTHING").format(
            cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
            vals=sql.SQL(", ").join(sql.Placeholder(c) for c in cols),
        )
        with self.pool.connection() as conn:
            conn.execute(stmt, params)

    def list_runs(self, limit: int = 20) -> list[dict[str, str]]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT %s", (limit,))
            return [_strs(r) for r in rows]
