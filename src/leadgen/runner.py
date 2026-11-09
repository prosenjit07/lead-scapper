"""High-level entry points shared by the CLI, scheduler and dashboard."""

from __future__ import annotations

from pathlib import Path

from leadgen.config import AppConfig, ConfigError, SearchConfig, Secrets
from leadgen.extract.industry import IndustryMap
from leadgen.http.fetcher import PoliteFetcher
from leadgen.models import SearchQuery
from leadgen.pipeline import Pipeline, RunResult
from leadgen.sources.registry import build_sources
from leadgen.store.base import LeadStore
from leadgen.store.state import CrawlState
from leadgen.validate.email import EmailValidator


def queries_for(search: SearchConfig) -> list[SearchQuery]:
    return [
        SearchQuery(
            keyword=kw,
            location=loc,
            search_name=search.name,
            industry=search.industry,
            osm_tags=list(search.osm_tags),
            max_results=search.max_results,
            tags=list(search.tags),
        )
        for loc in search.locations
        for kw in search.keywords
    ]


async def execute(
    cfg: AppConfig,
    secrets: Secrets,
    store: LeadStore,
    queries: list[SearchQuery],
    source_names: list[str],
    label: str,
    *,
    seed_file: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    force_recrawl: bool = False,
) -> RunResult:
    industries = IndustryMap.load(cfg.industries_file)
    state = CrawlState(cfg.state_path)
    try:
        async with PoliteFetcher(cfg.crawl, cfg.user_agent, cfg.user_agent_token) as fetcher:
            sources = build_sources(
                source_names, cfg=cfg, secrets=secrets, fetcher=fetcher, industries=industries,
                state=state, seed_file=seed_file,
            )  # fmt: skip
            if not sources:
                raise ConfigError(f"none of the sources {source_names} are usable (see warnings above)")
            pipeline = Pipeline(
                cfg=cfg, store=store, state=state, fetcher=fetcher, validator=EmailValidator(),
                industries=industries, dry_run=dry_run,
            )  # fmt: skip
            return await pipeline.run(queries, sources, label, limit=limit, force_recrawl=force_recrawl)
    finally:
        state.close()


async def run_search(
    cfg: AppConfig, secrets: Secrets, store: LeadStore, search: SearchConfig, **kwargs: object
) -> RunResult:
    sources = kwargs.pop("sources", None) or search.sources
    return await execute(cfg, secrets, store, queries_for(search), list(sources), search.name, **kwargs)  # type: ignore[arg-type]


async def run_seed_file(
    cfg: AppConfig,
    secrets: Secrets,
    store: LeadStore,
    path: Path,
    *,
    location: str = "",
    industry: str | None = None,
    tags: list[str] | None = None,
    **kwargs: object,
) -> RunResult:
    query = SearchQuery(keyword="", location=location, search_name=f"seed:{path.name}", industry=industry,
                        max_results=100_000, tags=tags or [])  # fmt: skip
    return await execute(
        cfg, secrets, store, [query], ["seed"], f"seed:{path.name}", seed_file=path, **kwargs
    )  # type: ignore[arg-type]
