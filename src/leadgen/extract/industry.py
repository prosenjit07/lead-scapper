"""Map source categories (OSM tags, Google place types, schema.org types, search keywords) to one
normalized industry taxonomy so leads can be organized consistently."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml


@dataclass
class Industry:
    name: str
    keywords: list[str] = field(default_factory=list)
    osm_tags: list[str] = field(default_factory=list)
    places_types: list[str] = field(default_factory=list)


def _load_yaml(text: str) -> dict[str, Industry]:
    raw = (yaml.safe_load(text) or {}).get("industries", {}) or {}
    out: dict[str, Industry] = {}
    for name, spec in raw.items():
        spec = spec or {}
        out[name] = Industry(
            name=name,
            keywords=[str(k).lower() for k in spec.get("keywords", []) or []],
            osm_tags=[str(t) for t in spec.get("osm_tags", []) or []],
            places_types=[str(t).lower() for t in spec.get("places_types", []) or []],
        )
    return out


class IndustryMap:
    def __init__(self, industries: dict[str, Industry]) -> None:
        self.industries = industries
        self._by_tag = {t.lower(): ind.name for ind in industries.values() for t in ind.osm_tags}
        self._by_type = {t: ind.name for ind in industries.values() for t in ind.places_types}
        # Longest keyword first so "dental clinic" beats "clinic".
        self._keywords = sorted(
            ((kw, ind.name) for ind in industries.values() for kw in ind.keywords), key=lambda x: -len(x[0])
        )

    @classmethod
    def load(cls, override_file: Path | None = None) -> IndustryMap:
        bundled = resources.files("leadgen.data").joinpath("industries.yaml").read_text(encoding="utf-8")
        industries = _load_yaml(bundled)
        if override_file:
            industries.update(_load_yaml(Path(override_file).read_text(encoding="utf-8")))
        return cls(industries)

    def match_keyword(self, text: str | None) -> Industry | None:
        if not text:
            return None
        t = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
        for kw, name in self._keywords:
            if f" {kw} " in t:
                return self.industries[name]
        return None

    def osm_tags_for(self, keyword: str, industry: str | None = None) -> list[str]:
        if industry and industry in self.industries and self.industries[industry].osm_tags:
            return list(self.industries[industry].osm_tags)
        match = self.match_keyword(keyword)
        return list(match.osm_tags) if match else []

    def classify(
        self,
        categories: list[str],
        keyword: str = "",
        override: str | None = None,
        schema_types: list[str] | None = None,
    ) -> str:
        if override:
            return override
        for cat in categories:
            c = cat.lower()
            if "=" in c and c in self._by_tag:
                return self._by_tag[c]
            if c in self._by_type:
                return self._by_type[c]
        for st in schema_types or []:
            if st.lower() in self._by_type:
                return self._by_type[st.lower()]
            if m := self.match_keyword(re.sub(r"(?<!^)(?=[A-Z])", " ", st)):
                return m.name
        if m := self.match_keyword(keyword):
            return m.name
        for cat in categories:  # unknown category: humanize it rather than lose it
            value = cat.split("=", 1)[-1]
            if value and value not in ("yes", "establishment", "point_of_interest"):
                return value.replace("_", " ").title()
        return keyword.strip().title() if keyword else ""
