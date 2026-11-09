"""Cron-style scheduling of configured searches (APScheduler, in-process).

Alternative without a long-running process: call `leads run --all` (or `leads run <search>`) from
cron / launchd / GitHub Actions. Both paths run the exact same pipeline."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from leadgen.config import AppConfig, load_config, load_secrets
from leadgen.logging_setup import get_logger
from leadgen.runner import run_search
from leadgen.store import open_store

log = get_logger("scheduler")


def run_scheduled(config_path: Path, search_name: str, store_kind: str | None = None) -> None:
    """One scheduled job. Config is re-read each time so edits apply without a restart."""
    try:
        cfg = load_config(config_path)
        secrets = load_secrets()
        store = open_store(cfg, secrets, store_kind)
        searches = cfg.searches if search_name == "all" else [cfg.get_search(search_name)]
        for search in searches:
            result = asyncio.run(run_search(cfg, secrets, store, search))
            log.info(
                f"scheduled run '{search.name}' done: {result.stats.new} new, {result.stats.errors} errors"
            )
    except Exception:
        log.exception(f"scheduled run '{search_name}' failed")  # keep the scheduler alive


def upcoming(cfg: AppConfig) -> list[tuple[str, str, datetime | None]]:
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    out = []
    for sch in cfg.schedules:
        trigger = CronTrigger.from_crontab(sch.cron, timezone=tz)
        out.append((sch.search, sch.cron, trigger.get_next_fire_time(None, now)))
    return out


def start(config_path: Path, store_kind: str | None = None) -> None:
    cfg = load_config(config_path)
    if not cfg.schedules:
        raise SystemExit("no schedules defined in config (add a `schedules:` section)")
    sched = BlockingScheduler(timezone=ZoneInfo(cfg.timezone))
    for i, sch in enumerate(cfg.schedules):
        sched.add_job(
            run_scheduled,
            CronTrigger.from_crontab(sch.cron, timezone=ZoneInfo(cfg.timezone)),
            args=[config_path, sch.search, store_kind],
            id=f"{sch.search}:{i}",
            name=f"leads run {sch.search}",
            max_instances=1,  # never overlap the same search
            coalesce=True,  # if several runs were missed (laptop asleep), run once
            misfire_grace_time=3600,
        )
    for search, cron, nxt in upcoming(cfg):
        log.info(f"scheduled '{search}' [{cron}] next run: {nxt}")
    log.info("scheduler running — Ctrl+C to stop")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
