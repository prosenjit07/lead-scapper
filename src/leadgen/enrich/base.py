"""Enrichment hook. Enrichers run after website extraction and before the final dedupe/write.
v1 ships none; add official-API enrichers (e.g. Hunter.io domain search for named contacts) by
implementing this protocol and passing them to Pipeline(enrichers=[...])."""

from __future__ import annotations

from typing import Protocol

from leadgen.models import Lead


class Enricher(Protocol):
    name: str

    async def enrich(self, lead: Lead) -> Lead:
        """Return the lead with extra fields filled. Must not overwrite non-empty fields."""
        ...
