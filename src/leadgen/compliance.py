"""Privacy / data-protection helpers.

- Suppression list: SHA-256 hashes of emails/domains that must never be (re)collected or contacted.
  Hashes let us honour opt-outs and erasure requests without keeping the personal data itself.
- forget(): GDPR Art. 17 erasure — delete matching leads + activities, then suppress.
- purge(): retention — delete stale, never-engaged leads; opt-outs become suppression hashes first.
- role_only policy: keep only shared role mailboxes (info@, sales@), drop named people.
This is engineering support for compliance, not legal advice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from leadgen.logging_setup import get_logger
from leadgen.models import ACTIVE_STATUSES, OPT_OUT_STATUSES, Lead, SuppressionEntry
from leadgen.normalize import registered_domain
from leadgen.store.base import LeadStore

log = get_logger("compliance")


def suppression_hash(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value.strip().lower()}".encode()).hexdigest()


class SuppressionList:
    def __init__(self, hashes: set[str] | None = None) -> None:
        self.hashes = set(hashes or ())

    def email_suppressed(self, email: str | None) -> bool:
        return bool(email) and suppression_hash("email", email) in self.hashes

    def domain_suppressed(self, domain: str | None) -> bool:
        return bool(domain) and suppression_hash("domain", domain) in self.hashes

    def add(self, kind: str, value: str) -> str:
        h = suppression_hash(kind, value)
        self.hashes.add(h)
        return h


def parse_identifier(identifier: str) -> tuple[str, str]:
    """'jane@acme.com' -> ('email', ...); 'acme.com' / 'https://acme.com' -> ('domain', 'acme.com')."""
    ident = identifier.strip().lower()
    if "@" in ident:
        return "email", ident.removeprefix("mailto:")
    domain = registered_domain(ident)
    if not domain:
        raise ValueError(f"'{identifier}' is neither an email address nor a domain")
    return "domain", domain


def _matches(lead: Lead, kind: str, value: str) -> bool:
    if kind == "email":
        return value in [e.lower() for e in lead.all_emails]
    return lead.domain.lower() == value or registered_domain(lead.website) == value


@dataclass
class ForgetResult:
    kind: str
    value: str
    leads_deleted: int
    emails_scrubbed: int


def forget(store: LeadStore, identifier: str, reason: str = "erasure request") -> ForgetResult:
    """Erase a person or company and prevent it from being collected again.
    email -> remove that address everywhere (delete the lead if it was the only contact route)
    domain -> delete every lead for the domain."""
    kind, value = parse_identifier(identifier)
    leads = store.load_leads()
    to_delete: list[str] = []
    scrub: dict[str, dict[str, str]] = {}
    for lead in leads:
        if not _matches(lead, kind, value):
            continue
        if kind == "domain" or (
            lead.email.lower() == value and not lead.additional_emails and not lead.website
        ):
            to_delete.append(lead.lead_id)
            continue
        remaining = [e for e in lead.all_emails if e.lower() != value]
        scrub[lead.lead_id] = {
            "email": remaining[0] if remaining else "",
            "additional_emails": ",".join(remaining[1:]),
            "email_status": lead.email_status if remaining and lead.email.lower() != value else "",
            "email_type": lead.email_type if remaining and lead.email.lower() != value else "",
            "contact_name": "" if lead.email.lower() == value else lead.contact_name,
            "contact_title": "" if lead.email.lower() == value else lead.contact_title,
        }
    if to_delete:
        store.delete_leads(to_delete)
        store.delete_activities(to_delete)
    if scrub:
        store.update_leads(scrub)
    store.add_suppression([SuppressionEntry(hash=suppression_hash(kind, value), kind=kind, reason=reason)])
    log.info("forgot identifier", extra={"kind": kind, "deleted": len(to_delete), "scrubbed": len(scrub)})
    return ForgetResult(kind, value, len(to_delete), len(scrub))


@dataclass
class PurgeResult:
    deleted: int
    suppressed: int
    lead_ids: list[str]


def purge(store: LeadStore, older_than_days: int, dry_run: bool = False) -> PurgeResult:
    """Delete leads not updated for N days unless they are in an active relationship.
    Opted-out / bounced leads are converted to suppression hashes before deletion."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    victims: list[Lead] = []
    for lead in store.load_leads():
        if lead.status in ACTIVE_STATUSES:
            continue
        stamp = lead.updated_at or lead.created_at
        try:
            ts = datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            continue
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            victims.append(lead)

    entries: list[SuppressionEntry] = []
    for lead in victims:
        if lead.status in OPT_OUT_STATUSES:
            for e in lead.all_emails:
                entries.append(
                    SuppressionEntry(suppression_hash("email", e), "email", f"purged:{lead.status}")
                )
            if lead.status == "do_not_contact" and lead.domain:
                entries.append(
                    SuppressionEntry(
                        suppression_hash("domain", lead.domain), "domain", "purged:do_not_contact"
                    )
                )

    ids = [lead.lead_id for lead in victims]
    if not dry_run and ids:
        if entries:
            store.add_suppression(entries)
        store.delete_leads(ids)
        store.delete_activities(ids)
    return PurgeResult(deleted=len(ids), suppressed=len(entries), lead_ids=ids)
