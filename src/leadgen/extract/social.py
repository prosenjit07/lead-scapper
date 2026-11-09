"""Recognize and normalize social profile links that a company publishes on its own website."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urlsplit

TWITTER_RESERVED = {
    "intent", "share", "home", "hashtag", "search", "i", "privacy", "tos", "login", "signup", "explore",
    "settings", "messages", "notifications", "compose", "widgets", "oauth",
}  # fmt: skip
FACEBOOK_RESERVED = {
    "sharer", "sharer.php", "share.php", "share", "dialog", "plugins", "tr", "login", "login.php",
    "policies", "privacy", "help", "events", "watch", "marketplace", "hashtag", "photo.php", "photo",
    "permalink.php", "story.php", "l.php", "ads", "business", "legal", "terms", "groups", "gaming",
}  # fmt: skip
INSTAGRAM_RESERVED = {
    "p",
    "reel",
    "reels",
    "explore",
    "accounts",
    "stories",
    "tv",
    "about",
    "developer",
    "legal",
}
# Handles that come from website templates / builders, not the business.
TEMPLATE_HANDLES = {
    "wix", "squarespace", "wordpress", "shopify", "godaddy", "elementor", "envato", "themeforest",
    "facebook", "twitter", "instagram", "linkedin", "yourpage", "username", "yourusername", "webflow",
}  # fmt: skip

TWITTER_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
INSTAGRAM_HANDLE = re.compile(r"^[A-Za-z0-9_.]{1,30}$")
SLUG = re.compile(r"^[A-Za-z0-9_.%\-]{1,100}$")


def _host_path(url: str) -> tuple[str, list[str], str]:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "", [], ""
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile.", "web.", "business."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    segments = [s for s in parts.path.split("/") if s]
    return host, segments, parts.query


def classify_social(url: str) -> tuple[str, str] | None:
    """Return (kind, normalized_url) for recognised profile links, else None.
    kind in: linkedin_company, linkedin_person, twitter, facebook, instagram."""
    host, seg, query = _host_path(url)
    if not host or not seg:
        return None

    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        if len(seg) >= 2 and seg[0] in ("company", "school", "showcase") and SLUG.match(seg[1]):
            if seg[1].lower() in TEMPLATE_HANDLES:
                return None
            return "linkedin_company", f"https://www.linkedin.com/{seg[0]}/{seg[1]}"
        if len(seg) >= 2 and seg[0] == "in" and SLUG.match(seg[1]):
            return "linkedin_person", f"https://www.linkedin.com/in/{seg[1]}"
        return None

    if host in ("twitter.com", "x.com"):
        handle = seg[0]
        if handle.lower() in TWITTER_RESERVED or handle.lower() in TEMPLATE_HANDLES:
            return None
        if TWITTER_HANDLE.match(handle):
            return "twitter", f"https://x.com/{handle}"
        return None

    if host in ("facebook.com", "fb.com"):
        first = seg[0].lower()
        if first == "profile.php":
            pid = parse_qs(query).get("id", [""])[0]
            return ("facebook", f"https://www.facebook.com/profile.php?id={pid}") if pid.isdigit() else None
        if first == "pages" and len(seg) >= 2:
            return "facebook", "https://www.facebook.com/" + "/".join(seg[:3])
        if first in FACEBOOK_RESERVED or first in TEMPLATE_HANDLES or not SLUG.match(seg[0]):
            return None
        return "facebook", f"https://www.facebook.com/{seg[0]}"

    if host == "instagram.com":
        handle = seg[0]
        if handle.lower() in INSTAGRAM_RESERVED or handle.lower() in TEMPLATE_HANDLES:
            return None
        if INSTAGRAM_HANDLE.match(handle):
            return "instagram", f"https://www.instagram.com/{handle}"
    return None


def extract_socials(urls: Iterable[str]) -> dict[str, str]:
    """First matching profile per kind, in document order."""
    found: dict[str, str] = {}
    for url in urls:
        hit = classify_social(url)
        if hit and hit[0] not in found:
            found[hit[0]] = hit[1]
    return found
