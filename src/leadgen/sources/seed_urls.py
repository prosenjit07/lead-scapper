"""Seed source: business websites you already have (one URL per line, or a CSV with a
`website`/`url` column and optional company_name, city, country, industry columns)."""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator
from pathlib import Path

from leadgen.models import Candidate, SearchQuery
from leadgen.normalize import normalize_url


def read_seed_file(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        reader = csv.DictReader(text.splitlines())
        rows = []
        for row in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            url = row.get("website") or row.get("url") or row.get("domain")
            if url:
                rows.append({**row, "website": url})
        return rows
    return [
        {"website": line.strip()}
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class SeedUrlSource:
    name = "seed"

    def __init__(self, path: Path | None = None, rows: list[dict[str, str]] | None = None) -> None:
        self.path = path
        self._rows = rows

    def available(self) -> tuple[bool, str]:
        if self._rows is None and (self.path is None or not self.path.exists()):
            return False, f"seed file not found: {self.path}"
        return True, ""

    async def discover(self, query: SearchQuery) -> AsyncIterator[Candidate]:
        rows = self._rows if self._rows is not None else read_seed_file(self.path)  # type: ignore[arg-type]
        for row in rows[: query.max_results]:
            url = normalize_url(row.get("website"))
            if not url:
                continue
            yield Candidate(
                source="seed",
                company_name=row.get("company_name") or row.get("company") or row.get("name") or None,
                website=url,
                city=row.get("city") or None,
                country=row.get("country") or None,
                industry=row.get("industry") or None,
                source_url=url,
            )
