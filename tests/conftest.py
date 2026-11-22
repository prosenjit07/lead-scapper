from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from leadgen.config import AppConfig, CrawlConfig
from leadgen.validate.email import EmailValidator, MxResult

FIXTURES = Path(__file__).parent / "fixtures"
# PostgreSQL tests run only when this points at a database you can create schemas in, e.g.
#   TEST_DATABASE_URL=postgresql:///leadgen_test uv run pytest
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def pg_dsn():
    """A fresh, migrated schema per test (dropped afterwards); skipped without TEST_DATABASE_URL."""
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run the PostgreSQL tests")
    psycopg = pytest.importorskip("psycopg")
    from leadgen.store import migrate

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in TEST_DATABASE_URL else "?"
    dsn = f"{TEST_DATABASE_URL}{sep}options=-csearch_path%3D{schema}"
    try:
        migrate.upgrade(dsn)
        yield dsn
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg_store(pg_dsn):
    from leadgen.store.postgres_store import PostgresStore

    store = PostgresStore.connect(pg_dsn, max_size=2)
    yield store
    store.close()


NO_MAIL_DOMAINS = {"deadmail.com"}
DNS_FAIL_DOMAINS = {"flaky-dns.com"}
A_ONLY_DOMAINS = {"aonly.com"}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def fake_mx(domain: str) -> MxResult:
    if domain in NO_MAIL_DOMAINS:
        return MxResult.NO_MAIL
    if domain in DNS_FAIL_DOMAINS:
        return MxResult.UNKNOWN
    if domain in A_ONLY_DOMAINS:
        return MxResult.A_ONLY
    return MxResult.HAS_MX


@pytest.fixture
def validator() -> EmailValidator:
    return EmailValidator(mx_lookup=fake_mx)


@pytest.fixture
def crawl_cfg() -> CrawlConfig:
    return CrawlConfig(per_domain_delay_s=0, backoff_base_s=0, retries=2, timeout_s=5, recrawl_after_days=30)


@pytest.fixture
def app_cfg(tmp_path: Path, crawl_cfg: CrawlConfig) -> AppConfig:
    return AppConfig(
        user_agent="TestLeadBot/1.0 (+https://test.invalid/bot)",
        store="sqlite",
        sqlite_path=tmp_path / "leads.db",
        state_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        crawl=crawl_cfg,
    )
