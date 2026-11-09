"""Domain models shared across sources, pipeline, stores and UI."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class LeadStatus(StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    CONTACTED = "contacted"
    REPLIED = "replied"
    MEETING = "meeting"
    WON = "won"
    LOST = "lost"
    BOUNCED = "bounced"
    UNSUBSCRIBED = "unsubscribed"
    DO_NOT_CONTACT = "do_not_contact"


# Statuses that mean "we have an active relationship" — never auto-purged by retention.
ACTIVE_STATUSES = {LeadStatus.CONTACTED, LeadStatus.REPLIED, LeadStatus.MEETING, LeadStatus.WON}
# Statuses that mean "never contact again" — converted to suppression entries when purged.
OPT_OUT_STATUSES = {LeadStatus.UNSUBSCRIBED, LeadStatus.DO_NOT_CONTACT, LeadStatus.BOUNCED}


class EmailStatus(StrEnum):
    VALID = "valid"  # syntax ok, domain accepts mail, matches the business domain
    RISKY = "risky"  # deliverable domain but free-mail / other domain / DNS inconclusive
    INVALID = "invalid"  # bad syntax, disposable, or domain cannot receive mail
    MISSING = "missing"  # no email found


class EmailType(StrEnum):
    PERSONAL = "personal"  # named mailbox, e.g. jane.doe@
    ROLE = "role"  # shared mailbox, e.g. info@, sales@
    FREE_MAIL = "free_mail"  # gmail.com, outlook.com, ...


@dataclass
class Candidate:
    """A raw business record yielded by a discovery source, before crawling/validation."""

    source: str
    company_name: str | None = None
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None
    categories: list[str] = field(default_factory=list)  # e.g. ["amenity=dentist"], ["dentist"]
    external_id: str | None = None
    source_url: str | None = None
    industry: str | None = None
    # When True, the source's terms restrict storing its content: only external_id/source_url are
    # stored and other facts must come from the business's own website.
    restricted: bool = False


@dataclass
class SearchQuery:
    keyword: str
    location: str
    search_name: str = "adhoc"
    industry: str | None = None
    osm_tags: list[str] = field(default_factory=list)
    max_results: int = 100
    tags: list[str] = field(default_factory=list)


class Lead(BaseModel):
    """One row per company. Field order == Google Sheet column order for new sheets."""

    model_config = ConfigDict(validate_assignment=False, extra="ignore")

    lead_id: str = ""
    status: str = LeadStatus.NEW.value
    company_name: str = ""
    website: str = ""
    domain: str = ""
    email: str = ""
    email_status: str = ""
    email_type: str = ""
    additional_emails: str = ""  # comma separated
    contact_name: str = ""
    contact_title: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    industry: str = ""
    linkedin_url: str = ""
    twitter_url: str = ""
    facebook_url: str = ""
    instagram_url: str = ""
    source: str = ""
    source_url: str = ""
    external_id: str = ""
    search_keyword: str = ""
    search_location: str = ""
    lawful_basis: str = ""
    tags: str = ""
    owner: str = ""
    notes: str = ""
    last_contacted_at: str = ""
    next_follow_up_at: str = ""
    campaign: str = ""
    sequence_step: str = ""
    run_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, object]) -> Lead:
        return cls(**{k: "" if row.get(k) is None else str(row.get(k)) for k in LEAD_COLUMNS})

    def to_row(self) -> dict[str, str]:
        return self.model_dump()

    @property
    def all_emails(self) -> list[str]:
        extra = [e.strip() for e in self.additional_emails.split(",") if e.strip()]
        return ([self.email] if self.email else []) + extra


LEAD_COLUMNS: list[str] = list(Lead.model_fields)

# Fields humans/CRM workflows own. Pipeline merges never overwrite these.
USER_OWNED_FIELDS = {
    "status", "owner", "notes", "tags", "last_contacted_at", "next_follow_up_at",
    "campaign", "sequence_step", "lead_id", "created_at", "run_id",
}  # fmt: skip
MERGEABLE_FIELDS = [f for f in LEAD_COLUMNS if f not in USER_OWNED_FIELDS and f != "updated_at"]

ACTIVITY_COLUMNS = ["lead_id", "timestamp", "type", "details", "campaign"]
SUPPRESSION_COLUMNS = ["hash", "kind", "reason", "added_at"]
RUN_COLUMNS = [
    "run_id", "started_at", "finished_at", "search", "sources", "discovered", "crawled",
    "new", "duplicates", "merged", "suppressed", "skipped", "errors", "notes",
]  # fmt: skip


@dataclass
class RunStats:
    run_id: str = field(
        default_factory=lambda: f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
    )
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str = ""
    search: str = ""
    sources: str = ""
    discovered: int = 0
    crawled: int = 0
    new: int = 0
    duplicates: int = 0
    merged: int = 0
    suppressed: int = 0
    skipped: int = 0
    errors: int = 0
    notes: str = ""

    def add_note(self, note: str) -> None:
        note = note.replace("\n", " ")[:300]
        self.notes = f"{self.notes}; {note}" if self.notes else note
        self.notes = self.notes[:2000]

    def to_row(self) -> dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}


@dataclass
class SuppressionEntry:
    hash: str
    kind: str  # "email" | "domain"
    reason: str
    added_at: str = field(default_factory=utcnow_iso)

    def to_row(self) -> dict[str, str]:
        return asdict(self)
