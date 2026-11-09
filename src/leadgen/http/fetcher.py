"""PoliteFetcher: every outbound request goes through robots.txt, per-host rate limiting, and retries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from leadgen.config import CrawlConfig
from leadgen.http.ratelimit import DomainRateLimiter
from leadgen.http.robots import RobotsCache
from leadgen.logging_setup import get_logger

log = get_logger("fetcher")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_REDIRECTS = 5
JS_SHELL_TEXT_THRESHOLD = 400  # visible characters below which a page is probably a JS-only shell


class RetryableStatusError(Exception):
    def __init__(self, status: int, url: str, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.retry_after = retry_after


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    content_type: str


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        from datetime import UTC, datetime

        return max(0.0, (dt - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableStatusError | httpx.TransportError)


class PoliteFetcher:
    def __init__(
        self,
        cfg: CrawlConfig,
        user_agent: str,
        ua_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en;q=0.9,*;q=0.5",
            },
            timeout=httpx.Timeout(cfg.timeout_s),
            follow_redirects=False,
        )
        self.user_agent = user_agent
        self.robots = RobotsCache(self.client, ua_token)
        self.limiter = DomainRateLimiter(cfg.per_domain_delay_s)
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._renderer: Any = None

    async def __aenter__(self) -> PoliteFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._renderer is not None:
            await self._renderer.close()
            self._renderer = None
        if self._owns_client:
            await self.client.aclose()

    # ------------------------------------------------------------------ helpers
    def _wait(self, retry_state: Any) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, RetryableStatusError) and exc.retry_after is not None:
            return min(exc.retry_after, 60.0)
        return min(self.cfg.backoff_base_s * (2 ** (retry_state.attempt_number - 1)), 30.0)

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            stop=stop_after_attempt(self.cfg.retries),
            wait=self._wait,
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

    # ------------------------------------------------------------------ API calls
    async def api_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Call a public API (Overpass, Nominatim, Places). Rate-limited + retried; robots.txt not applied
        because these are documented APIs governed by their own usage policies."""
        host = urlsplit(url).hostname or ""
        kwargs.setdefault("follow_redirects", True)
        kwargs["headers"] = {"Accept": "application/json", **(kwargs.get("headers") or {})}
        async for attempt in self._retrying():
            with attempt:
                async with self.limiter.slot(host), self._sem:
                    resp = await self.client.request(method, url, **kwargs)
                if resp.status_code in RETRYABLE_STATUS:
                    raise RetryableStatusError(
                        resp.status_code, url, _parse_retry_after(resp.headers.get("retry-after"))
                    )
                resp.raise_for_status()
                return resp
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ web pages
    async def allowed(self, url: str) -> bool:
        return await self.robots.can_fetch(url)

    async def fetch_page(self, url: str) -> FetchResult | None:
        """Fetch an HTML page politely. Returns None when disallowed, non-HTML, or an error status.
        Redirects are followed manually so robots.txt is checked for every hop."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if urlsplit(current).scheme not in ("http", "https"):
                return None
            if not await self.robots.can_fetch(current):
                log.debug("robots.txt disallows", extra={"url": current})
                return None
            host = urlsplit(current).hostname or ""
            delay = await self.robots.crawl_delay(current)
            if delay is not None and delay > self.limiter.delay_for(host):
                self.limiter.set_delay(host, delay)

            result, redirect = await self._get_once(current)
            if redirect:
                current = redirect
                continue
            if result and self.cfg.render_js and _looks_like_js_shell(result.text):
                rendered = await self._render(result.url)
                if rendered:
                    result = rendered
            return result
        log.debug("too many redirects", extra={"url": url})
        return None

    async def _get_once(self, url: str) -> tuple[FetchResult | None, str | None]:
        host = urlsplit(url).hostname or ""
        async for attempt in self._retrying():
            with attempt:
                async with self.limiter.slot(host), self._sem, self.client.stream("GET", url) as resp:
                    status = resp.status_code
                    if status in RETRYABLE_STATUS:
                        raise RetryableStatusError(
                            status, url, _parse_retry_after(resp.headers.get("retry-after"))
                        )
                    if 300 <= status < 400 and resp.headers.get("location"):
                        return None, urljoin(url, resp.headers["location"])
                    ctype = resp.headers.get("content-type", "").lower()
                    if status >= 400 or ("html" not in ctype and ctype):
                        return None, None
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) >= self.cfg.max_bytes:
                            break
                    encoding = resp.charset_encoding or "utf-8"
                    text = bytes(body[: self.cfg.max_bytes]).decode(encoding, errors="replace")
                    return FetchResult(url=str(resp.url), status=status, text=text, content_type=ctype), None
        raise AssertionError("unreachable")  # pragma: no cover

    async def _render(self, url: str) -> FetchResult | None:
        """Optional JS rendering for pages that are empty shells (requires the [browser] extra)."""
        try:
            if self._renderer is None:
                from leadgen.http.browser import BrowserRenderer

                self._renderer = BrowserRenderer(self.user_agent, timeout_s=self.cfg.timeout_s)
                await self._renderer.start()
            host = urlsplit(url).hostname or ""
            async with self.limiter.slot(host), self._sem:
                final_url, html = await self._renderer.render(url)
            return FetchResult(url=final_url, status=200, text=html, content_type="text/html")
        except ImportError:
            log.warning("render_js is enabled but playwright is not installed: uv sync --extra browser")
            self.cfg.render_js = False
        except Exception as e:
            log.debug("render failed", extra={"url": url, "error": str(e)})
        return None


def _looks_like_js_shell(html: str) -> bool:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return len(soup.get_text(" ", strip=True)) < JS_SHELL_TEXT_THRESHOLD
