"""Configuration: YAML app config (searches, crawl, compliance) + secrets from environment / .env."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/config.yaml")
KNOWN_SOURCES = {"osm", "google_places", "seed"}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


class CrawlConfig(BaseModel):
    max_pages_per_site: int = Field(6, ge=1, le=25)
    per_domain_delay_s: float = Field(2.0, ge=0)
    concurrency: int = Field(10, ge=1, le=50)
    timeout_s: float = Field(15.0, gt=0)
    max_bytes: int = Field(2_000_000, gt=10_000)
    retries: int = Field(3, ge=1, le=8)
    backoff_base_s: float = Field(1.0, ge=0)
    recrawl_after_days: int = Field(30, ge=0)
    render_js: bool = False  # needs the [browser] extra + `playwright install chromium`


class ComplianceConfig(BaseModel):
    # allow: keep named/personal business emails; role_only: keep only role addresses (info@, sales@, ...)
    personal_data: Literal["allow", "role_only"] = "allow"
    retention_days: int = Field(365, ge=1)
    lawful_basis: str = "legitimate_interest"
    require_email: bool = False


class OsmConfig(BaseModel):
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    timeout_s: int = 90
    point_radius_m: int = 10_000  # used when a location geocodes to a point instead of an area


class GooglePlacesConfig(BaseModel):
    # Google Maps Platform terms restrict storing Places content (place IDs are exempt).
    # false = Places is used for discovery only; stored facts come from the business's own website.
    store_place_details: bool = False
    language_code: str = "en"


class SearchConfig(BaseModel):
    name: str
    keywords: list[str] = Field(min_length=1)
    locations: list[str] = Field(min_length=1)
    sources: list[str] = Field(default_factory=lambda: ["osm"])
    industry: str | None = None
    osm_tags: list[str] = Field(default_factory=list)
    max_results: int = Field(100, ge=1, le=1000)  # per keyword x location x source
    tags: list[str] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, v: list[str]) -> list[str]:
        unknown = set(v) - KNOWN_SOURCES
        if unknown:
            raise ValueError(f"unknown sources {sorted(unknown)}; known: {sorted(KNOWN_SOURCES)}")
        return v


class ScheduleConfig(BaseModel):
    search: str  # a search name, or "all"
    cron: str  # standard 5-field crontab expression


class AppConfig(BaseModel):
    user_agent: str
    store: Literal["sheets", "sqlite"] = "sheets"
    sqlite_path: Path = Path("data/leads.db")
    state_path: Path = Path("data/state.db")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    timezone: str = "UTC"
    merge_duplicates: bool = True
    industries_file: Path | None = None
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)
    osm: OsmConfig = Field(default_factory=OsmConfig)
    google_places: GooglePlacesConfig = Field(default_factory=GooglePlacesConfig)
    searches: list[SearchConfig] = Field(default_factory=list)
    schedules: list[ScheduleConfig] = Field(default_factory=list)

    @field_validator("user_agent")
    @classmethod
    def _ua_identifies_bot(cls, v: str) -> str:
        if len(v.strip()) < 8 or "/" not in v:
            raise ValueError(
                'user_agent must look like "BotName/1.0 (+https://your-site/bot; you@your-site)"'
            )
        return v.strip()

    @model_validator(mode="after")
    def _schedules_reference_searches(self) -> AppConfig:
        names = {s.name for s in self.searches}
        if len(names) != len(self.searches):
            raise ValueError("search names must be unique")
        for sch in self.schedules:
            if sch.search != "all" and sch.search not in names:
                raise ValueError(f"schedule references unknown search '{sch.search}'")
        return self

    @property
    def user_agent_token(self) -> str:
        """Product token used for robots.txt group matching, e.g. 'AcmeLeadBot'."""
        return self.user_agent.split("/", 1)[0].split()[0]

    @property
    def user_agent_is_placeholder(self) -> bool:
        return "example.com" in self.user_agent or "example.org" in self.user_agent

    def get_search(self, name: str) -> SearchConfig:
        for s in self.searches:
            if s.name == name:
                return s
        raise ConfigError(f"no search named '{name}' in config (have: {[s.name for s in self.searches]})")


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_service_account_file: Path | None = None
    google_sheet_id: str | None = None
    google_places_api_key: str | None = None


def resolve_config_path(path: str | Path | None) -> Path:
    if path:
        return Path(path)
    return Path(os.environ.get("LEADGEN_CONFIG", DEFAULT_CONFIG_PATH))


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path} (run `leads init` to create one)")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {cfg_path}: {e}") from e
    try:
        return AppConfig.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"invalid config {cfg_path}:\n{e}") from e


def load_secrets() -> Secrets:
    return Secrets()
