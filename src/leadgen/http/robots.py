"""robots.txt cache (RFC 9309 semantics) backed by Protego."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from protego import Protego

from leadgen.logging_setup import get_logger

log = get_logger("robots")


@dataclass
class _Entry:
    fetched_at: float
    parser: Protego | None
    allow_all: bool  # True when robots.txt is missing (4xx) -> everything allowed
    deny_all: bool  # True when robots.txt is unreachable (5xx/network) -> assume disallowed


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, ua_token: str, ttl_s: float = 24 * 3600) -> None:
        self._client = client
        self._ua = ua_token
        self._ttl = ttl_s
        self._cache: dict[str, _Entry] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _entry(self, url: str) -> _Entry:
        origin = origin_of(url)
        async with self._locks[origin]:
            cached = self._cache.get(origin)
            if cached and time.monotonic() - cached.fetched_at < self._ttl:
                return cached
            entry = await self._fetch(origin)
            self._cache[origin] = entry
            return entry

    async def _fetch(self, origin: str) -> _Entry:
        now = time.monotonic()
        robots_url = f"{origin}/robots.txt"
        try:
            resp = await self._client.get(robots_url, follow_redirects=True, timeout=10.0)
        except httpx.HTTPError as e:
            log.info(
                "robots.txt unreachable, treating site as disallowed",
                extra={"url": robots_url, "error": str(e)},
            )
            return _Entry(now, None, allow_all=False, deny_all=True)
        if resp.status_code == 200:
            try:
                return _Entry(now, Protego.parse(resp.text), allow_all=False, deny_all=False)
            except Exception as e:  # malformed robots.txt -> be conservative
                log.info(
                    "robots.txt unparsable, treating site as disallowed",
                    extra={"url": robots_url, "error": str(e)},
                )
                return _Entry(now, None, allow_all=False, deny_all=True)
        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            return _Entry(now, None, allow_all=True, deny_all=False)
        log.info(
            "robots.txt returned server error, treating site as disallowed",
            extra={"url": robots_url, "status": resp.status_code},
        )
        return _Entry(now, None, allow_all=False, deny_all=True)

    async def can_fetch(self, url: str) -> bool:
        entry = await self._entry(url)
        if entry.deny_all:
            return False
        if entry.allow_all or entry.parser is None:
            return True
        return bool(entry.parser.can_fetch(url, self._ua))

    async def crawl_delay(self, url: str) -> float | None:
        entry = await self._entry(url)
        if entry.parser is None:
            return None
        delay = entry.parser.crawl_delay(self._ua)
        rate = entry.parser.request_rate(self._ua)
        if rate and rate.requests:
            rate_delay = rate.seconds / rate.requests
            delay = max(delay or 0.0, rate_delay)
        return float(delay) if delay else None
