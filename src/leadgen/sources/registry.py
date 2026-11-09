"""Build source instances by name. Register new sources here."""

from __future__ import annotations

from pathlib import Path

from leadgen.config import AppConfig, Secrets
from leadgen.extract.industry import IndustryMap
from leadgen.http.fetcher import PoliteFetcher
from leadgen.logging_setup import get_logger
from leadgen.sources.base import Source
from leadgen.sources.google_places import GooglePlacesSource
from leadgen.sources.osm_overpass import OsmOverpassSource
from leadgen.sources.seed_urls import SeedUrlSource
from leadgen.store.state import CrawlState

log = get_logger("sources")


def build_source(
    name: str,
    *,
    cfg: AppConfig,
    secrets: Secrets,
    fetcher: PoliteFetcher,
    industries: IndustryMap,
    state: CrawlState | None,
    seed_file: Path | None = None,
) -> Source:
    if name == "osm":
        return OsmOverpassSource(fetcher, cfg.osm, industries, state)
    if name == "google_places":
        return GooglePlacesSource(fetcher, secrets.google_places_api_key, cfg.google_places)
    if name == "seed":
        return SeedUrlSource(seed_file)
    raise ValueError(f"unknown source '{name}'")


def build_sources(names: list[str], **kwargs: object) -> list[Source]:
    """Instantiate sources, skipping (with a warning) any that are unavailable, e.g. missing API key."""
    out: list[Source] = []
    for name in dict.fromkeys(names):
        src = build_source(name, **kwargs)  # type: ignore[arg-type]
        ok, reason = src.available()
        if ok:
            out.append(src)
        else:
            log.warning(f"skipping source '{name}': {reason}")
    return out
