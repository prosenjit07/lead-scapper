"""Duplicate detection. A lead is a duplicate when ANY key matches an existing lead:
  email:<primary email>   domain:<registered domain>   name:<normalized company>|<normalized city>
Company name alone is not used (many "Smile Dental"s exist), so name keys include the city."""

from __future__ import annotations

import hashlib

from leadgen.models import Lead
from leadgen.normalize import normalize_company, normalize_place, registered_domain


def dedupe_keys(
    *,
    email: str | None = None,
    website: str | None = None,
    domain: str | None = None,
    company_name: str | None = None,
    city: str | None = None,
) -> list[str]:
    keys: list[str] = []
    if email:
        keys.append(f"email:{email.strip().lower()}")
    d = domain or (registered_domain(website) if website else None)
    if d:
        keys.append(f"domain:{d.lower()}")
    name, place = normalize_company(company_name), normalize_place(city)
    if name and place:
        keys.append(f"name:{name}|{place}")
    return keys


def lead_keys(lead: Lead) -> list[str]:
    return dedupe_keys(
        email=lead.email,
        website=lead.website,
        domain=lead.domain,
        company_name=lead.company_name,
        city=lead.city,
    )


def make_lead_id(keys: list[str]) -> str:
    """Stable id from the strongest key (domain > email > name). Prefixed so Sheets keeps it as text."""
    if not keys:
        raise ValueError("cannot build a lead id without any identifying key")
    order = {"domain": 0, "email": 1, "name": 2}
    best = sorted(keys, key=lambda k: order.get(k.split(":", 1)[0], 9))[0]
    return "ld_" + hashlib.sha1(best.encode("utf-8")).hexdigest()[:12]


class DedupeIndex:
    def __init__(self) -> None:
        self._owner: dict[str, str] = {}  # key -> lead_id

    @classmethod
    def from_leads(cls, leads: list[Lead]) -> DedupeIndex:
        idx = cls()
        for lead in leads:
            if lead.lead_id:
                idx.add(lead_keys(lead), lead.lead_id)
        return idx

    def find(self, keys: list[str], ignore: str | None = None) -> str | None:
        for k in keys:
            owner = self._owner.get(k)
            if owner and owner != ignore:
                return owner
        return None

    def add(self, keys: list[str], lead_id: str) -> None:
        for k in keys:
            self._owner.setdefault(k, lead_id)

    def __len__(self) -> int:
        return len(self._owner)
