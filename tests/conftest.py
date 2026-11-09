from __future__ import annotations

from pathlib import Path

import pytest

from leadgen.config import AppConfig, CrawlConfig
from leadgen.validate.email import EmailValidator, MxResult

FIXTURES = Path(__file__).parent / "fixtures"

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
