"""Per-host politeness: at most one in-flight request per host and a minimum gap between requests."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

MAX_HOST_DELAY_S = 60.0  # cap absurd crawl-delay values so a run can finish


class DomainRateLimiter:
    def __init__(self, default_delay_s: float) -> None:
        self.default_delay_s = default_delay_s
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_finished: dict[str, float] = {}
        self._delays: dict[str, float] = {}

    def set_delay(self, host: str, delay_s: float) -> None:
        """Set a host-specific delay (e.g. robots.txt crawl-delay or an API's usage policy)."""
        self._delays[host] = min(MAX_HOST_DELAY_S, max(0.0, delay_s))

    def delay_for(self, host: str) -> float:
        return self._delays.get(host, self.default_delay_s)

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        async with self._locks[host]:
            last = self._last_finished.get(host)
            if last is not None:
                wait = last + self.delay_for(host) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                yield
            finally:
                self._last_finished[host] = time.monotonic()
