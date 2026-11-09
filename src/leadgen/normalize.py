"""Normalization helpers: URLs/domains, countries, phone numbers, company and place names."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

import phonenumbers
import pycountry
import tldextract

# Bundled public-suffix snapshot (no network). Private suffixes make github.io / myshopify.com /
# wixsite.com subdomains count as separate sites.
_TLD = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

# Hosts that are never a business's own website: social networks, link-in-bio, maps, review sites.
NON_WEBSITE_DOMAINS = {
    "fb.com", "fb.me", "x.com", "t.co", "youtu.be", "linktr.ee", "linkin.bio", "beacons.ai", "goo.gl",
    "g.page", "g.co", "wa.me", "t.me", "bit.ly", "tinyurl.com", "threads.net", "bsky.app", "airbnb.com",
    "foursquare.com", "yell.com", "trustpilot.com", "glassdoor.com", "indeed.com", "calendly.com",
}  # fmt: skip
NON_WEBSITE_LABELS = {
    "facebook", "instagram", "linkedin", "twitter", "tiktok", "youtube", "pinterest", "google",
    "tripadvisor", "yelp", "booking", "whatsapp", "telegram", "amazon", "ebay", "etsy",
}  # fmt: skip
# Hosting platforms missing from the public suffix list where each subdomain is a different business.
SUBDOMAIN_PLATFORMS = {
    "business.site", "godaddysites.com", "square.site", "webflow.io", "carrd.co", "jimdofree.com",
    "jimdosite.com", "weebly.com", "wordpress.com", "site123.me", "mystrikingly.com", "webnode.page",
    "ueniweb.com", "wixsite.com", "netlify.app", "vercel.app",
}  # fmt: skip


def _host_of(url_or_host: str) -> str | None:
    s = url_or_host.strip()
    if not s:
        return None
    if "//" not in s:
        s = "//" + s
    try:
        host = urlsplit(s).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def normalize_url(url: str | None) -> str | None:
    """Return a clean absolute http(s) URL (no fragment), or None if unusable."""
    if not url:
        return None
    url = url.strip().strip("'\"<>")
    if not url or url.startswith(("mailto:", "tel:", "javascript:")):
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or "." not in host or _is_ip(host):
        return None
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def site_root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@lru_cache(maxsize=50_000)
def registered_domain(url_or_host: str | None) -> str | None:
    """'https://www.acme.co.uk/contact' -> 'acme.co.uk'; platform subdomains keep their host."""
    if not url_or_host:
        return None
    host = _host_of(url_or_host)
    if not host or _is_ip(host):
        return None
    ext = _TLD(host)
    rd = getattr(ext, "top_domain_under_public_suffix", None) or ext.registered_domain
    if not rd:
        return None
    if rd in SUBDOMAIN_PLATFORMS and ext.subdomain:
        fqdn = ext.fqdn
        return fqdn[4:] if fqdn.startswith("www.") else fqdn
    return rd.lower()


def is_non_website(url: str | None) -> bool:
    """True for social / maps / link-shortener URLs that should not be crawled as a business site."""
    if not url:
        return False
    host = _host_of(url)
    if not host:
        return False
    ext = _TLD(host)
    rd = (getattr(ext, "top_domain_under_public_suffix", None) or ext.registered_domain or "").lower()
    return rd in NON_WEBSITE_DOMAINS or ext.domain.lower() in NON_WEBSITE_LABELS


def same_site(url: str, domain: str | None) -> bool:
    return bool(domain) and registered_domain(url) == domain


# ---------------------------------------------------------------------------- text / names
def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def clean_text(value: str | None, max_len: int = 300) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value[:max_len]


LEGAL_SUFFIXES = {
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation", "co", "company", "gmbh",
    "ag", "plc", "llp", "lp", "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "oy", "ab", "aps", "kg",
    "ug", "pty", "pvt", "pte", "sl", "kft", "sro", "the", "and", "group", "holdings", "intl",
}  # fmt: skip


def normalize_company(name: str | None) -> str:
    """Canonical company name for duplicate detection: 'The Smile Dental Co., Ltd.' -> 'smile dental'."""
    if not name:
        return ""
    s = ascii_fold(name).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = s.split()
    kept = [t for t in tokens if t not in LEGAL_SUFFIXES]
    return " ".join(kept or tokens)


def normalize_place(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", ascii_fold(value).lower()).strip()


# ---------------------------------------------------------------------------- countries
_COUNTRY_ALIASES = {
    "uk": "GB", "u.k.": "GB", "great britain": "GB", "britain": "GB", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB", "usa": "US", "u.s.a.": "US", "u.s.": "US", "america": "US",
    "united states of america": "US", "uae": "AE", "russia": "RU", "south korea": "KR", "korea": "KR",
    "vietnam": "VN", "iran": "IR", "turkey": "TR", "czech republic": "CZ", "the netherlands": "NL",
    "holland": "NL", "bolivia": "BO", "venezuela": "VE", "tanzania": "TZ", "taiwan": "TW", "syria": "SY",
    "laos": "LA", "moldova": "MD", "deutschland": "DE", "espana": "ES", "españa": "ES",
}  # fmt: skip


@lru_cache(maxsize=2048)
def country_code(value: str | None) -> str | None:
    """Map 'UK', 'gb', 'United Kingdom', 'GBR' -> 'GB'. Returns None when unknown."""
    if not value:
        return None
    v = value.strip().strip(".").lower()
    if not v:
        return None
    if v in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[v]
    try:
        return pycountry.countries.lookup(v).alpha_2
    except LookupError:
        return None


def country_name(value: str | None) -> str:
    """Normalize any country representation to a readable name ('GB' -> 'United Kingdom')."""
    if not value:
        return ""
    code = country_code(value)
    if not code:
        return clean_text(value, 80)
    c = pycountry.countries.get(alpha_2=code)
    if c is None:
        return clean_text(value, 80)
    return getattr(c, "common_name", None) or c.name


def split_location(location: str) -> tuple[str, str]:
    """'Bristol, UK' -> ('Bristol', 'United Kingdom'); 'Berlin' -> ('Berlin', '')."""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return ("", country_name(parts[0])) if country_code(parts[0]) else (parts[0], "")
    country = country_name(parts[-1]) if country_code(parts[-1]) else ""
    return parts[0], country


# ---------------------------------------------------------------------------- phones
def normalize_phone(raw: str | None, country: str | None = None) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    region = country_code(country) if country else None
    try:
        num = phonenumbers.parse(raw, region)
        if phonenumbers.is_possible_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    cleaned = re.sub(r"[^\d+()\- ]", "", raw).strip()
    return cleaned[:30] if len(re.sub(r"\D", "", cleaned)) >= 6 else ""
