"""Discovery source protocol. A source turns (keyword, location) into raw business Candidates.
New sources (directories you've confirmed allow automated access, search APIs, CSV feeds) only
need to implement this and be registered in sources/registry.py."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from leadgen.models import Candidate, SearchQuery


class SourceError(Exception):
    """A source failed in a way worth surfacing to the user (bad API key, quota, invalid query)."""


class Source(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(usable, reason-if-not). E.g. missing API key."""
        ...

    def discover(self, query: SearchQuery) -> AsyncIterator[Candidate]: ...
