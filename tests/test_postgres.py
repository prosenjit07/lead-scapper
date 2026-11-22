"""PostgreSQL-only behaviour: constraints, schema checks, `leads db …` commands and the SQLite import.
Skipped unless TEST_DATABASE_URL is set (see conftest.pg_dsn)."""

from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

from conftest import TEST_DATABASE_URL
from leadgen.compliance import suppression_hash
from leadgen.config import AppConfig, ConfigError, Secrets
from leadgen.models import Lead, RunStats, SuppressionEntry
from leadgen.store.sqlite_store import SQLiteStore

psycopg = pytest.importorskip("psycopg")
from leadgen.store import migrate, open_store  # noqa: E402
from leadgen.store.postgres_store import PostgresStore  # noqa: E402
from leadgen.store.transfer import copy_store  # noqa: E402

runner = CliRunner()


def lead(lead_id: str, **kw) -> Lead:
    base = dict(lead_id=lead_id, company_name=lead_id, created_at="2026-11-01T00:00:00+00:00",
                updated_at="2026-11-01T00:00:00+00:00")  # fmt: skip
    return Lead(**{**base, **kw})


# ---------------------------------------------------------------------------- constraints
def test_one_lead_per_domain_and_email(pg_store):
    assert pg_store.add_leads([lead("ld_a", domain="acme.com", email="info@acme.com")]) == 1
    # same domain or same email (any case) under another id: rejected, even from a concurrent run
    assert pg_store.add_leads([lead("ld_b", domain="acme.com"), lead("ld_c", email="INFO@acme.com")]) == 0
    assert pg_store.add_leads([lead("ld_d", domain="other.com"), lead("ld_e")]) == 2  # blanks don't clash
    assert sorted(x.lead_id for x in pg_store.load_leads()) == ["ld_a", "ld_d", "ld_e"]


def test_conflicting_update_is_skipped_without_losing_the_others(pg_store):
    pg_store.add_leads([lead("ld_a", domain="acme.com"), lead("ld_b"), lead("ld_c")])
    n = pg_store.update_leads({"ld_b": {"domain": "acme.com"}, "ld_c": {"notes": "kept"}})
    assert n == 1
    leads = {x.lead_id: x for x in pg_store.load_leads()}
    assert leads["ld_b"].domain == "" and leads["ld_c"].notes == "kept"


def test_invalid_values_are_rejected(pg_store):
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_store.add_leads([lead("ld_a", status="maybe")])
    pg_store.add_leads([lead("ld_b")])
    with pytest.raises(ValueError, match="next_follow_up_at"):
        pg_store.update_leads({"ld_b": {"next_follow_up_at": "next tuesday"}})


def test_timestamps(pg_store):
    pg_store.add_leads([lead("ld_a")])
    pg_store.update_leads({"ld_a": {"next_follow_up_at": "2026-01-02", "last_contacted_at": ""}})
    got = pg_store.load_leads()[0]
    assert got.next_follow_up_at == "2026-01-02T00:00:00+00:00"  # dates come back as UTC midnight
    assert got.last_contacted_at == ""  # blank stays blank (NULL)
    pg_store.add_leads([Lead(lead_id="ld_blank")])  # created_at/updated_at are required: filled with now
    assert next(x for x in pg_store.load_leads() if x.lead_id == "ld_blank").created_at


def test_activity_for_unknown_lead_is_refused(pg_store):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg_store.add_activity("ld_missing", "email_sent")


# ---------------------------------------------------------------------------- schema / connection
def test_unmigrated_database_asks_for_migrate():
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run the PostgreSQL tests")
    schema = "test_unmigrated_check"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE; CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in TEST_DATABASE_URL else "?"
    dsn = f"{TEST_DATABASE_URL}{sep}options=-csearch_path%3D{schema}"
    try:
        with pytest.raises(ConfigError, match="leads db migrate"):
            PostgresStore.connect(dsn)
        migrate.upgrade(dsn)
        PostgresStore.connect(dsn).close()
        migrate.downgrade(dsn, "base")
        with pytest.raises(ConfigError, match="leads db migrate"):
            PostgresStore.connect(dsn)
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_open_store_errors(tmp_path, pg_dsn):
    cfg = AppConfig(user_agent="TestLeadBot/1.0 (+https://test.invalid/bot)", store="postgres",
                    sqlite_path=tmp_path / "l.db", state_path=tmp_path / "s.db", log_dir=tmp_path / "logs")  # fmt: skip
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        open_store(cfg, Secrets(_env_file=None, database_url=None))
    with pytest.raises(ConfigError, match="cannot connect"):
        open_store(cfg, Secrets(_env_file=None, database_url="postgresql:///leadgen_no_such_database_xyz"))
    store = open_store(cfg, Secrets(_env_file=None, database_url=pg_dsn))
    assert store.name == "postgres"
    store.close()


# ---------------------------------------------------------------------------- import from SQLite
@pytest.fixture
def sqlite_source(tmp_path):
    src = SQLiteStore(tmp_path / "old.db")
    src.add_leads([
        lead("ld_old", domain="acme.com", email="info@acme.com", created_at="2026-10-01T00:00:00+00:00"),
        lead("ld_dupe", domain="acme.com", created_at="2026-10-05T00:00:00+00:00"),  # same domain, newer
        lead("ld_other", domain="other.com", status="contacted", last_contacted_at="2026-11-05T10:00:00+00:00"),
    ])  # fmt: skip
    src.add_suppression([SuppressionEntry(suppression_hash("email", "gone@x.com"), "email", "erasure request",
                                          "2026-10-02T00:00:00+00:00")])  # fmt: skip
    src.log_run(RunStats(run_id="run_1", started_at="2026-10-01T06:00:00+00:00", search="dental-uk", new=3))
    src.add_activity("ld_other", "email_sent", "intro", "q1")
    src.add_activity("ld_deleted", "call")  # orphan: its lead no longer exists
    yield src
    src.close()


def test_import_copies_everything_once(sqlite_source, pg_store):
    report = copy_store(sqlite_source, pg_store)
    assert report.ok
    counts = {k: (v.source, v.copied, v.skipped) for k, v in report.tables.items()}
    assert counts == {
        "leads": (3, 2, 1),
        "suppression": (1, 1, 0),
        "runs": (1, 1, 0),
        "activities": (2, 1, 1),
    }
    assert any("ld_dupe" in n and "ld_old" in n for n in report.notes)  # the oldest lead wins
    assert sorted(x.lead_id for x in pg_store.load_leads()) == ["ld_old", "ld_other"]
    other = next(x for x in pg_store.load_leads() if x.lead_id == "ld_other")
    assert (other.status, other.last_contacted_at) == ("contacted", "2026-11-05T10:00:00+00:00")
    assert pg_store.list_suppression()[0].reason == "erasure request"
    assert pg_store.list_runs(5)[0]["new"] == "3"
    assert pg_store.list_activities("ld_other")[0]["details"] == "intro"

    again = copy_store(sqlite_source, pg_store)  # re-running copies nothing twice
    assert {k: v.copied for k, v in again.tables.items()} == dict.fromkeys(counts, 0)
    assert len(pg_store.list_activities(None)) == 1


def test_import_dry_run_and_blocking_problems(sqlite_source, pg_store):
    dry = copy_store(sqlite_source, pg_store, dry_run=True)
    assert dry.ok and pg_store.load_leads() == []
    sqlite_source.update_leads({"ld_other": {"status": "maybe", "next_follow_up_at": "soon"}})
    bad = copy_store(sqlite_source, pg_store)
    assert not bad.ok and pg_store.load_leads() == []  # nothing written
    assert any("status 'maybe'" in p for p in bad.problems) and any("'soon'" in p for p in bad.problems)


# ---------------------------------------------------------------------------- CLI and API on PostgreSQL
def test_cli_db_commands_and_postgres_store(tmp_path, monkeypatch, pg_dsn, sqlite_source):
    monkeypatch.chdir(tmp_path)  # no .env here: DATABASE_URL comes from the environment
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    cfg = {"user_agent": "TestLeadBot/1.0 (+https://test.invalid/bot)", "store": "postgres",
           "sqlite_path": str(sqlite_source.path), "state_path": str(tmp_path / "state.db"),
           "log_dir": str(tmp_path / "logs")}  # fmt: skip
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    base = ["--config", str(tmp_path / "config.yaml")]

    r = runner.invoke(app_cli(), [*base, "db", "migrate"])
    assert r.exit_code == 0 and "already up to date" in r.output, r.output
    r = runner.invoke(app_cli(), [*base, "db", "import", "--dry-run"])
    assert r.exit_code == 0 and "DRY RUN" in r.output, r.output
    r = runner.invoke(app_cli(), [*base, "db", "import"])
    assert r.exit_code == 0 and "ld_dupe" in r.output, r.output
    r = runner.invoke(app_cli(), [*base, "db", "status"])
    assert r.exit_code == 0 and "schema 0001" in r.output, r.output
    r = runner.invoke(app_cli(), [*base, "list", "--format", "json"])
    assert r.exit_code == 0 and "ld_other" in r.output, r.output
    r = runner.invoke(app_cli(), [*base, "status", "other.com", "won", "--note", "signed"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app_cli(), [*base, "doctor"])
    assert "PostgreSQL" in r.output and "schema 0001" in r.output, r.output


def test_cli_import_postgres_to_postgres(tmp_path, monkeypatch, pg_dsn, pg_dsn2, sqlite_source):
    """e.g. moving from a local PostgreSQL to a hosted one (Neon): SOURCE_DATABASE_URL → DATABASE_URL."""
    source = PostgresStore.connect(pg_dsn)
    copy_store(sqlite_source, source)
    source.add_activity("ld_other", "reply", "interested")
    monkeypatch.chdir(tmp_path)
    cfg = {"user_agent": "TestLeadBot/1.0 (+https://test.invalid/bot)", "store": "postgres",
           "state_path": str(tmp_path / "state.db"), "log_dir": str(tmp_path / "logs")}  # fmt: skip
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    base = ["--config", str(tmp_path / "config.yaml"), "db", "import", "--from", "postgres"]

    monkeypatch.setenv("DATABASE_URL", pg_dsn2)
    r = runner.invoke(app_cli(), base)
    assert r.exit_code != 0 and "SOURCE_DATABASE_URL" in r.output  # source is required
    monkeypatch.setenv("SOURCE_DATABASE_URL", pg_dsn2)
    r = runner.invoke(app_cli(), base)
    assert r.exit_code != 0 and "same database" in r.output  # never copy a database onto itself

    monkeypatch.setenv("SOURCE_DATABASE_URL", pg_dsn)
    r = runner.invoke(app_cli(), base)
    assert r.exit_code == 0, r.output
    dest = PostgresStore.connect(pg_dsn2)
    try:
        assert {x.lead_id: x.to_row() for x in dest.load_leads()} == {
            x.lead_id: x.to_row() for x in source.load_leads()
        }
        assert dest.list_activities(None) == source.list_activities(None)
        assert dest.list_runs(10) == source.list_runs(10)
        assert runner.invoke(app_cli(), base).exit_code == 0  # re-run: nothing duplicated
        assert len(dest.list_activities(None)) == 2
    finally:
        dest.close()
        source.close()


def app_cli():
    from leadgen.cli import app

    return app


def test_api_on_postgres(tmp_path, pg_dsn, pg_store):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from leadgen.api import create_app

    pg_store.add_leads([lead("ld_a", domain="acme.com", email="info@acme.com", email_status="valid")])
    cfg = AppConfig(user_agent="TestLeadBot/1.0 (+https://test.invalid/bot)", store="postgres",
                    sqlite_path=tmp_path / "l.db", state_path=tmp_path / "s.db", log_dir=tmp_path / "logs")  # fmt: skip
    with TestClient(create_app(cfg, Secrets(_env_file=None, database_url=pg_dsn))) as client:
        assert client.get("/health").json()["store"] == "postgres"
        assert client.get("/leads").json()["total"] == 1
        r = client.patch("/leads/acme.com", json={"status": "contacted", "next_follow_up_at": "2026-12-01"})
        assert r.status_code == 200 and r.json()["last_contacted_at"], r.text
        assert client.get("/leads/ld_a/activities").json()[0]["type"] == "status_change"
