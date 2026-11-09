"""Optional headless-browser renderer for JS-only sites. Install with: uv sync --extra browser &&
uv run playwright install chromium. Only used for pages already approved by robots.txt."""

from __future__ import annotations

from typing import Any

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


class BrowserRenderer:
    def __init__(self, user_agent: str, timeout_s: float = 15.0) -> None:
        self._ua = user_agent
        self._timeout_ms = int(timeout_s * 1000)
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(user_agent=self._ua, java_script_enabled=True)

        async def _block_heavy(route: Any) -> None:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await self._context.route("**/*", _block_heavy)

    async def render(self, url: str) -> tuple[str, str]:
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=self._timeout_ms)
            return page.url, await page.content()
        finally:
            await page.close()

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
