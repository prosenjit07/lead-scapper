"""Crawl a business website (homepage + a few contact/about pages) and extract public contact data."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from leadgen.extract.emails import emails_from_mailto, emails_from_text, has_obfuscated_email
from leadgen.extract.jsonld import JsonLdInfo, parse_jsonld
from leadgen.extract.social import extract_socials
from leadgen.http.fetcher import PoliteFetcher
from leadgen.logging_setup import get_logger
from leadgen.normalize import clean_text, normalize_url, registered_domain, site_root

log = get_logger("website")

# Ordered by usefulness; the first patterns win when the page budget is small.
PAGE_PRIORITIES: list[tuple[int, re.Pattern[str]]] = [
    (0, re.compile(r"contact|kontakt|contacto|contatti|get-in-touch|reach-us|enquir", re.I)),
    (1, re.compile(r"impressum|imprint|mentions-legales|legal-notice|aviso-legal", re.I)),
    (2, re.compile(r"about|ueber-uns|uber-uns|qui-sommes|chi-siamo|who-we-are|our-story|company", re.I)),
    (3, re.compile(r"team|staff|people|our-people|leadership|management|meet-", re.I)),
]
SKIP_LINK_RE = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3)$|/(wp-admin|wp-login|login|signin|sign-in|"
    r"account|cart|checkout|my-account|admin|feed|tag|category)(/|$)|[?&](replytocom|share)=",
    re.I,
)
TITLE_SPLIT_RE = re.compile(r"\s+[|\-–—:·•]\s+")


@dataclass
class WebsiteData:
    start_url: str
    final_url: str = ""
    pages: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    email_pages: dict[str, str] = field(default_factory=dict)  # email -> page where first seen
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    jsonld: JsonLdInfo = field(default_factory=JsonLdInfo)
    site_name: str = ""
    obfuscated_email_seen: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.pages)


def _site_name(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content"):
        return clean_text(og["content"], 120)
    if soup.title and soup.title.string:
        first = TITLE_SPLIT_RE.split(soup.title.string.strip())[0]
        name = clean_text(first, 120)
        if name.lower() not in {"home", "homepage", "welcome", "index"}:
            return name
    return ""


class WebsiteCrawler:
    def __init__(self, fetcher: PoliteFetcher, max_pages: int = 6) -> None:
        self.fetcher = fetcher
        self.max_pages = max_pages

    async def crawl(self, url: str) -> WebsiteData:
        start = normalize_url(url)
        data = WebsiteData(start_url=url)
        if not start:
            data.error = "invalid url"
            return data

        home = await self.fetcher.fetch_page(start)
        if home is None:
            data.error = "unreachable, non-HTML, or disallowed by robots.txt"
            return data

        data.final_url = site_root(home.url)
        domain = registered_domain(home.url)
        queue = self._parse_page(home.url, home.text, data, domain)

        for page_url in queue[: self.max_pages - 1]:
            try:
                page = await self.fetcher.fetch_page(page_url)
            except Exception as e:  # one bad sub-page must not lose the rest of the site
                log.debug("sub-page failed", extra={"url": page_url, "error": str(e)})
                continue
            if page is not None and registered_domain(page.url) == domain:
                self._parse_page(page.url, page.text, data, domain)
        return data

    def _parse_page(self, url: str, html: str, data: WebsiteData, domain: str | None) -> list[str]:
        """Extract everything from one page; return prioritized same-site links to visit next."""
        data.pages.append(url)
        soup = BeautifulSoup(html, "lxml")

        page_ld = parse_jsonld(soup)
        data.jsonld.merge(page_ld)
        if not data.site_name:
            data.site_name = _site_name(soup)
        if has_obfuscated_email(html):
            data.obfuscated_email_seen = True

        hrefs = [a.get("href", "").strip() for a in soup.find_all("a", href=True)]
        socials = extract_socials([urljoin(url, h) for h in hrefs] + page_ld.same_as)
        for k, v in socials.items():
            data.socials.setdefault(k, v)

        for h in hrefs:
            if h.lower().startswith("tel:"):
                phone = clean_text(h[4:], 40)
                if phone and phone not in data.phones:
                    data.phones.append(phone)

        page_emails = emails_from_mailto(hrefs)
        for tag in soup(["script", "style", "noscript", "template", "svg"]):
            tag.decompose()
        page_emails += [e for e in emails_from_text(soup.get_text(" ")) if e not in page_emails]
        page_emails += [e for e in page_ld.emails if e not in page_emails]
        for e in page_emails:
            if e not in data.emails:
                data.emails.append(e)
                data.email_pages[e] = url

        return self._rank_links(url, hrefs, domain, set(data.pages))

    def _rank_links(self, base: str, hrefs: list[str], domain: str | None, seen: set[str]) -> list[str]:
        ranked: dict[str, int] = {}
        seen_keys = {s.rstrip("/") for s in seen}
        for h in hrefs:
            if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absolute = urldefrag(urljoin(base, h))[0]
            if registered_domain(absolute) != domain or SKIP_LINK_RE.search(absolute):
                continue
            key = absolute.rstrip("/")
            if urlsplit(absolute).scheme not in ("http", "https") or key in seen_keys:
                continue
            if any(k.rstrip("/") == key for k in ranked):
                continue
            path = urlsplit(absolute).path.lower()
            for prio, pattern in PAGE_PRIORITIES:
                if pattern.search(path):
                    ranked[absolute] = min(prio, ranked.get(absolute, prio))
                    break
        return sorted(ranked, key=lambda u: (ranked[u], len(u)))
