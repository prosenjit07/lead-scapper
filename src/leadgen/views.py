"""Filtering / grouping / status helpers shared by the CLI, the dashboard and the REST API."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime

from leadgen.models import OPT_OUT_STATUSES, Lead, LeadStatus, utcnow_iso
from leadgen.normalize import ascii_fold

OPT_OUT_VALUES = {s.value for s in OPT_OUT_STATUSES}


def _eq(value: str, wanted: Iterable[str] | None) -> bool:
    if not wanted:
        return True
    wanted_l = {w.strip().lower() for w in wanted if w and w.strip()}
    return not wanted_l or value.strip().lower() in wanted_l


def filter_leads(
    leads: Iterable[Lead],
    *,
    industry: Iterable[str] | None = None,
    country: Iterable[str] | None = None,
    city: Iterable[str] | None = None,
    status: Iterable[str] | None = None,
    email_status: Iterable[str] | None = None,
    source: Iterable[str] | None = None,
    text: str | None = None,
    contactable_only: bool = False,
) -> list[Lead]:
    needle = (text or "").strip().lower()
    out = []
    for lead in leads:
        if not (
            _eq(lead.industry, industry)
            and _eq(lead.country, country)
            and _eq(lead.city, city)
            and _eq(lead.status, status)
            and _eq(lead.email_status, email_status)
            and _eq(lead.source, source)
        ):
            continue  # fmt: skip
        if contactable_only and (
            not lead.email or lead.email_status == "invalid" or lead.status in OPT_OUT_VALUES
        ):
            continue
        if (
            needle
            and needle
            not in " ".join(
                [lead.company_name, lead.email, lead.domain, lead.contact_name, lead.notes, lead.tags]
            ).lower()
        ):
            continue
        out.append(lead)
    return out


def count_by(leads: Iterable[Lead], field: str) -> list[tuple[str, int]]:
    c = Counter((getattr(lead, field) or "(blank)") for lead in leads)
    return c.most_common()


def find_lead(leads: Iterable[Lead], identifier: str) -> Lead | None:
    ident = identifier.strip().lower()
    for lead in leads:
        if ident in (lead.lead_id.lower(), lead.email.lower(), lead.domain.lower()):
            return lead
    return None


def append_note(notes: str, note: str) -> str:
    """Existing notes plus a new line `[YYYY-MM-DD] note`."""
    return (notes + "\n" if notes else "") + f"[{datetime.now(UTC):%Y-%m-%d}] {note}"


def status_change(lead: Lead, new_status: str, note: str | None = None) -> tuple[dict[str, str], str]:
    """Fields to write and the Activities `details` text for a pipeline status change.
    Moving to `contacted` stamps last_contacted_at; a note is appended to notes with today's date."""
    new_status = str(LeadStatus(new_status).value)
    fields = {"status": new_status}
    if new_status == LeadStatus.CONTACTED:
        fields["last_contacted_at"] = utcnow_iso()
    if note:
        fields["notes"] = append_note(lead.notes, note)
    details = f"{lead.status} -> {new_status}" + (f": {note}" if note else "")
    return fields, details


def export_filename(filters: dict[str, object], fmt: str) -> str:
    """e.g. leads_berlin_valid_2026-11-22.csv: the filters that were applied, then the date."""
    parts = ["leads"]
    for key in ("industry", "country", "city", "status", "email_status", "source"):
        parts += [_slug(str(getattr(v, "value", v))) for v in filters.get(key) or []]  # type: ignore[union-attr]
    if filters.get("text"):
        parts.append(_slug(str(filters["text"])))
    name = "_".join(p for p in parts if p)[:80].rstrip("_-")
    return f"{name}_{datetime.now(UTC):%Y-%m-%d}.{fmt}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", ascii_fold(value).lower()).strip("-")
