"""Email extraction from public pages: mailto: links and plain visible text only.

Deliberately NOT decoded: Cloudflare email protection, "name [at] domain [dot] com" and similar
obfuscation. A site owner who hides an address is signalling they don't want it harvested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import unquote

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})"
    r"(?![A-Za-z0-9-])"
)
OBFUSCATION_RE = re.compile(
    r"data-cfemail|__cf_email__|\w\s*[\[\(\{]\s*at\s*[\]\)\}]\s*\w|\w\s+at\s+\w+\s+dot\s+\w", re.I
)

FILE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "bmp", "ico", "tif", "tiff", "css", "js", "mjs",
    "json", "map", "mp4", "webm", "mov", "pdf", "woff", "woff2", "ttf", "eot", "php", "html", "htm",
    "asp", "aspx",
}  # fmt: skip
PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "yoursite.com",
    "yourcompany.com", "company.com", "email.com", "mysite.com", "website.com", "test.com",
    "placeholder.com", "domain.tld", "address.com", "sentry.io", "wixpress.com", "localhost",
}  # fmt: skip
PLACEHOLDER_DOMAIN_SUFFIXES = (".sentry.io", ".wixpress.com", ".example.com", ".invalid", ".local", ".test")
UNWANTED_LOCAL_PARTS = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster",
    "hostmaster", "abuse", "bounce", "bounces", "root", "nobody", "example", "test", "user", "username",
    "name", "your", "yourname", "youremail", "email", "firstname.lastname", "john.doe", "jane.doe",
    "johndoe", "janedoe", "someone", "you",
}  # fmt: skip
HEX_RE = re.compile(r"^[0-9a-f]{16,}$")


def clean_email(raw: str) -> str | None:
    """Normalize and filter an email candidate. Returns None for junk/placeholder/system addresses."""
    email = raw.strip().strip(".,;:'\"()[]<>").lower()
    if email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if not local or not domain or "." not in domain or ".." in email:
        return None
    if domain.rsplit(".", 1)[-1] in FILE_EXTENSIONS:
        return None  # e.g. logo@2x.png
    if domain in PLACEHOLDER_DOMAINS or domain.endswith(PLACEHOLDER_DOMAIN_SUFFIXES):
        return None
    if local in UNWANTED_LOCAL_PARTS or HEX_RE.match(local):
        return None
    if len(email) > 254 or len(local) > 64:
        return None
    return email


def emails_from_mailto(hrefs: Iterable[str]) -> list[str]:
    out: list[str] = []
    for href in hrefs:
        if not href.lower().startswith("mailto:"):
            continue
        target = unquote(href[7:]).split("?", 1)[0]
        for part in re.split(r"[,;\s]+", target):
            if (e := clean_email(part)) and e not in out:
                out.append(e)
    return out


def emails_from_text(text: str) -> list[str]:
    out: list[str] = []
    for m in EMAIL_RE.finditer(text):
        if (e := clean_email(m.group(1))) and e not in out:
            out.append(e)
    return out


def has_obfuscated_email(html: str) -> bool:
    return bool(OBFUSCATION_RE.search(html))
