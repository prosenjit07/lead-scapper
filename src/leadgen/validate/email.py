"""Email validation without mailbox probing.

Checks: RFC syntax (email-validator), disposable domains, MX/null-MX via DNS (cached per domain),
role vs personal vs free-mail classification, and whether the domain matches the business website.
SMTP RCPT probing is intentionally not done: it is unreliable and gets sending IPs blacklisted.
Plug a paid verifier in later by wrapping EmailValidator.check().
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib import resources

import dns.asyncresolver
import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, validate_email

from leadgen.models import EmailStatus, EmailType
from leadgen.normalize import registered_domain


class MxResult(StrEnum):
    HAS_MX = "has_mx"
    A_ONLY = "a_only"  # no MX but an A record: RFC 5321 implicit MX, often unreliable
    NO_MAIL = "no_mail"  # NXDOMAIN, null MX, or no records at all
    UNKNOWN = "unknown"  # DNS timeout / server failure


ROLE_LOCAL_PARTS = {
    "info", "contact", "contacts", "hello", "hi", "hey", "sales", "office", "admin", "administration",
    "enquiries", "enquiry", "inquiries", "inquiry", "support", "help", "team", "mail", "reception",
    "bookings", "booking", "book", "reservations", "service", "services", "customerservice", "marketing",
    "careers", "jobs", "hr", "recruitment", "recruiting", "accounts", "billing", "finance", "invoices",
    "press", "media", "partners", "partnerships", "general", "studio", "clinic", "appointments",
    "frontdesk", "newbusiness", "business", "biz", "orders", "shop", "store", "feedback", "webmaster",
    "privacy", "legal", "dpo", "gdpr", "compliance", "events", "kontakt", "office1", "welcome", "ask",
    "care", "practice", "surgery", "school", "admissions", "headteacher", "principal", "secretary",
}  # fmt: skip
# Departments, mailing lists and system mailboxes: shared (not a person) and poor cold-outreach targets.
LOW_VALUE_ROLES = {
    "careers", "jobs", "hr", "recruitment", "recruiting", "privacy", "legal", "dpo", "gdpr", "compliance",
    "webmaster", "billing", "invoices", "accounts", "press", "media", "feedback", "support", "help",
    "security", "abuse", "conduct", "announce", "announcements", "list", "lists", "news", "newsletter",
    "users", "devel", "request", "owner", "bounces", "notifications", "alerts", "sponsors",
    "sponsorship", "donate", "donations", "membership", "members", "volunteer", "volunteers", "board",
    "community", "editor", "editors", "web", "website", "it", "tech", "technical", "ops", "infra",
    "sysadmin", "noc", "unsubscribe", "subscribe", "returns", "complaints",
}  # fmt: skip
ROLE_TOKENS = ROLE_LOCAL_PARTS | LOW_VALUE_ROLES
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.co.in",
    "ymail.com", "rocketmail.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de", "outlook.com",
    "outlook.de", "live.com", "live.co.uk", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "pm.me", "gmx.de", "gmx.net", "gmx.com", "gmx.at", "web.de", "mail.com",
    "yandex.ru", "yandex.com", "zoho.com", "mail.ru", "qq.com", "163.com", "126.com", "t-online.de",
    "orange.fr", "free.fr", "laposte.net", "libero.it", "btinternet.com", "sky.com", "virginmedia.com",
    "comcast.net", "verizon.net", "att.net", "fastmail.com", "hey.com", "tutanota.com", "seznam.cz",
    "wp.pl", "o2.pl", "rediffmail.com", "bigpond.com", "shaw.ca", "rogers.com", "sbcglobal.net",
}  # fmt: skip
LOCAL_TOKEN_SPLIT = re.compile(r"[._+\-]")


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    text = resources.files("leadgen.data").joinpath("disposable_domains.txt").read_text(encoding="utf-8")
    return frozenset(
        line.strip().lower() for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


async def dns_mx_lookup(domain: str) -> MxResult:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 6.0
    try:
        answer = await resolver.resolve(domain, "MX")
        exchanges = [str(r.exchange).rstrip(".") for r in answer]
        if not any(exchanges):  # RFC 7505 null MX: "this domain accepts no mail"
            return MxResult.NO_MAIL
        return MxResult.HAS_MX
    except dns.resolver.NXDOMAIN:
        return MxResult.NO_MAIL
    except dns.resolver.NoAnswer:
        try:
            await resolver.resolve(domain, "A")
            return MxResult.A_ONLY
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return MxResult.NO_MAIL
        except dns.exception.DNSException:
            return MxResult.UNKNOWN
    except dns.exception.DNSException:
        return MxResult.UNKNOWN


def email_type(email: str) -> EmailType:
    local, _, domain = email.partition("@")
    if domain in FREE_MAIL_DOMAINS:
        return EmailType.FREE_MAIL
    # Any department/list token makes it shared: "sales.uk@", "python-announce@", "info2@".
    tokens = {re.sub(r"\d+$", "", t) for t in LOCAL_TOKEN_SPLIT.split(local) if t}
    if local in ROLE_TOKENS or tokens & ROLE_TOKENS:
        return EmailType.ROLE
    return EmailType.PERSONAL


def is_low_value(email: str) -> bool:
    local = email.partition("@")[0]
    tokens = {re.sub(r"\d+$", "", t) for t in LOCAL_TOKEN_SPLIT.split(local) if t}
    return local in LOW_VALUE_ROLES or bool(tokens & LOW_VALUE_ROLES)


@dataclass
class EmailCheck:
    email: str
    status: EmailStatus
    type: EmailType
    reason: str
    domain: str
    matches_site: bool = False

    def score(self, prefer_personal: bool = True) -> int:
        if self.status == EmailStatus.INVALID:
            return -1000
        s = 100 if self.status == EmailStatus.VALID else 50
        s += 20 if self.matches_site else 0
        if self.type == EmailType.PERSONAL:
            s += 10 if prefer_personal else 0
        elif self.type == EmailType.ROLE:
            s += 5
        if is_low_value(self.email):
            s -= 30
        return s


MxLookup = Callable[[str], Awaitable[MxResult]]


class EmailValidator:
    def __init__(self, mx_lookup: MxLookup | None = dns_mx_lookup, check_mx: bool = True) -> None:
        self._mx_lookup = mx_lookup
        self._check_mx = check_mx and mx_lookup is not None
        self._cache: dict[str, MxResult] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _mx(self, domain: str) -> MxResult:
        if not self._check_mx or self._mx_lookup is None:
            return MxResult.HAS_MX
        async with self._locks[domain]:
            if domain not in self._cache:
                self._cache[domain] = await self._mx_lookup(domain)
            return self._cache[domain]

    async def check(self, email: str, site_domain: str | None = None) -> EmailCheck:
        raw = email.strip()
        try:
            normalized = validate_email(raw, check_deliverability=False).normalized.lower()
        except EmailNotValidError as e:
            return EmailCheck(raw.lower(), EmailStatus.INVALID, EmailType.PERSONAL, f"syntax: {e}", "")

        domain = normalized.partition("@")[2]
        etype = email_type(normalized)
        matches = bool(site_domain) and registered_domain(domain) == site_domain

        if domain in disposable_domains() or registered_domain(domain) in disposable_domains():
            return EmailCheck(normalized, EmailStatus.INVALID, etype, "disposable domain", domain, matches)

        mx = await self._mx(domain)
        if mx == MxResult.NO_MAIL:
            return EmailCheck(
                normalized, EmailStatus.INVALID, etype, "domain does not accept mail", domain, matches
            )
        if mx == MxResult.UNKNOWN:
            return EmailCheck(normalized, EmailStatus.RISKY, etype, "DNS inconclusive", domain, matches)
        if mx == MxResult.A_ONLY:
            return EmailCheck(normalized, EmailStatus.RISKY, etype, "no MX record (A only)", domain, matches)
        if etype == EmailType.FREE_MAIL:
            return EmailCheck(normalized, EmailStatus.RISKY, etype, "free-mail provider", domain, matches)
        if site_domain and not matches:
            return EmailCheck(
                normalized, EmailStatus.RISKY, etype, "domain differs from website", domain, matches
            )
        return EmailCheck(normalized, EmailStatus.VALID, etype, "ok", domain, matches)

    async def check_many(self, emails: list[str], site_domain: str | None) -> list[EmailCheck]:
        seen: set[str] = set()
        out: list[EmailCheck] = []
        for e in emails:
            if e.lower() in seen:
                continue
            seen.add(e.lower())
            out.append(await self.check(e, site_domain))
        return out


def rank(checks: list[EmailCheck], prefer_personal: bool = True) -> list[EmailCheck]:
    """Best first; invalid addresses removed. Stable for ties (keeps discovery order)."""
    usable = [c for c in checks if c.status != EmailStatus.INVALID]
    return sorted(usable, key=lambda c: -c.score(prefer_personal))
