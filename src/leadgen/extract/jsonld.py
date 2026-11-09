"""schema.org JSON-LD parsing: the business's own structured self-description."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from leadgen.normalize import clean_text

NON_ORG_TYPES = {
    "website", "webpage", "breadcrumblist", "listitem", "imageobject", "searchaction", "person", "article",
    "blogposting", "newsarticle", "product", "offer", "review", "aggregaterating", "faqpage", "question",
    "answer", "videoobject", "event", "postaladdress", "geocoordinates", "openinghoursspecification",
    "contactpoint", "sitenavigationelement", "collectionpage", "itemlist", "readaction", "entrypoint",
    "aboutpage", "contactpage", "howto", "recipe", "rating", "service", "propertyvalue", "place",
}  # fmt: skip
ORG_TYPE_HINTS = (
    "organization",
    "business",
    "store",
    "clinic",
    "agency",
    "office",
    "practice",
    "service",
    "school",
    "dentist",
    "restaurant",
    "hotel",
    "physician",
    "attorney",
    "legal",
    "company",
)
# Keys under which a Person is NOT a contact of the business (article authors, reviewers, ...).
NON_CONTACT_KEYS = {
    "author",
    "creator",
    "reviewedby",
    "review",
    "editor",
    "contributor",
    "publisher",
    "accountablePerson".lower(),
    "copyrightholder",
    "sdpublisher",
}


@dataclass
class Person:
    name: str
    job_title: str = ""
    email: str = ""


@dataclass
class JsonLdInfo:
    org_name: str = ""
    org_types: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    telephones: list[str] = field(default_factory=list)
    street: str = ""
    city: str = ""
    region: str = ""
    postcode: str = ""
    country: str = ""
    same_as: list[str] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)

    def merge(self, other: JsonLdInfo) -> None:
        for attr in ("org_name", "street", "city", "region", "postcode", "country"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        for attr in ("org_types", "emails", "telephones", "same_as"):
            mine = getattr(self, attr)
            mine.extend(x for x in getattr(other, attr) if x not in mine)
        known = {p.name.lower() for p in self.people}
        self.people.extend(p for p in other.people if p.name.lower() not in known)


def _types(node: dict[str, Any]) -> list[str]:
    t = node.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return [str(x).split("/")[-1].lower() for x in t if x]


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return clean_text(str(value), 200) if value else ""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _is_org(node: dict[str, Any]) -> bool:
    types = _types(node)
    if not types or not node.get("name"):
        return False
    if any(t in NON_ORG_TYPES for t in types) and not any(h in t for t in types for h in ("business",)):
        return False
    return any(h in t for t in types for h in ORG_TYPE_HINTS) or bool(
        node.get("address") or node.get("telephone")
    )


def _person(node: dict[str, Any]) -> Person | None:
    name = _text(node.get("name"))
    if not name or len(name.split()) > 5 or re.search(r"\d|@|http", name):
        return None
    email = _text(node.get("email")).removeprefix("mailto:").lower()
    return Person(name=name, job_title=_text(node.get("jobTitle")), email=email)


def _apply_address(info: JsonLdInfo, addr: Any) -> None:
    for a in _as_list(addr):
        if isinstance(a, str):
            info.street = info.street or clean_text(a, 200)
            continue
        if not isinstance(a, dict):
            continue
        info.street = info.street or _text(a.get("streetAddress"))
        info.city = info.city or _text(a.get("addressLocality"))
        info.region = info.region or _text(a.get("addressRegion"))
        info.postcode = info.postcode or _text(a.get("postalCode"))
        info.country = info.country or _text(a.get("addressCountry"))


def _walk(node: Any, parent_key: str, info: JsonLdInfo) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, parent_key, info)
        return
    if not isinstance(node, dict):
        return

    types = _types(node)
    if "person" in types:
        p = _person(node) if parent_key not in NON_CONTACT_KEYS else None
        if p and p.name.lower() not in {x.name.lower() for x in info.people}:
            info.people.append(p)
        return  # don't descend into people (their worksFor etc. adds noise)

    if _is_org(node) and parent_key not in NON_CONTACT_KEYS:
        info.org_name = info.org_name or _text(node.get("name") or node.get("legalName"))
        raw_types = [str(x).split("/")[-1] for x in _as_list(node.get("@type")) if x]
        info.org_types.extend(t for t in raw_types if t not in info.org_types)
        for e in _as_list(node.get("email")):
            e = _text(e).removeprefix("mailto:").lower()
            if e and e not in info.emails:
                info.emails.append(e)
        for t in _as_list(node.get("telephone")):
            t = _text(t)
            if t and t not in info.telephones:
                info.telephones.append(t)
        _apply_address(info, node.get("address"))
        for s in _as_list(node.get("sameAs")):
            if isinstance(s, str) and s.startswith("http") and s not in info.same_as:
                info.same_as.append(s)
        for cp in _as_list(node.get("contactPoint")):
            if isinstance(cp, dict):
                for e in _as_list(cp.get("email")):
                    e = _text(e).removeprefix("mailto:").lower()
                    if e and e not in info.emails:
                        info.emails.append(e)
                for t in _as_list(cp.get("telephone")):
                    t = _text(t)
                    if t and t not in info.telephones:
                        info.telephones.append(t)

    for key, value in node.items():
        if key.startswith("@") and key != "@graph":
            continue
        if isinstance(value, dict | list):
            _walk(value, key.lower(), info)


def parse_jsonld(soup: BeautifulSoup) -> JsonLdInfo:
    info = JsonLdInfo()
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            # Some sites put several objects or trailing commas in one block; try a lenient fix.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)
            except json.JSONDecodeError:
                continue
        _walk(data, "", info)
    return info
