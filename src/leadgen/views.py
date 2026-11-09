"""Filtering / grouping helpers shared by the CLI and the dashboard."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from leadgen.models import OPT_OUT_STATUSES, Lead

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
