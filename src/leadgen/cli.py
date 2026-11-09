"""`leads` command-line interface."""

from __future__ import annotations

import asyncio
import csv
import functools
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, TypeVar

import httpx
import typer
from rich.console import Console
from rich.table import Table

from leadgen.config import (
    KNOWN_SOURCES,
    AppConfig,
    ConfigError,
    SearchConfig,
    Secrets,
    load_config,
    load_secrets,
    resolve_config_path,
)
from leadgen.logging_setup import setup_logging
from leadgen.models import LEAD_COLUMNS, Lead, LeadStatus, utcnow_iso
from leadgen.pipeline import RunResult
from leadgen.store import open_store
from leadgen.store.base import LeadStore

app = typer.Typer(
    help="Compliant B2B lead generation: discover businesses, crawl their public pages, validate and store leads.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
sheet_app = typer.Typer(help="Google Sheet setup.", no_args_is_help=True)
scheduler_app = typer.Typer(help="Scheduled runs.", no_args_is_help=True)
app.add_typer(sheet_app, name="sheet")
app.add_typer(scheduler_app, name="scheduler")

console = Console()
err = Console(stderr=True)
F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class Ctx:
    config_path: Path
    store_kind: str | None
    verbose: bool
    _cfg: AppConfig | None = None
    _secrets: Secrets | None = None
    _store: LeadStore | None = field(default=None, repr=False)

    @property
    def cfg(self) -> AppConfig:
        if self._cfg is None:
            self._cfg = load_config(self.config_path)
            setup_logging(self._cfg.log_dir, "DEBUG" if self.verbose else self._cfg.log_level)
        return self._cfg

    @property
    def secrets(self) -> Secrets:
        if self._secrets is None:
            self._secrets = load_secrets()
        return self._secrets

    @property
    def store(self) -> LeadStore:
        if self._store is None:
            self._store = open_store(self.cfg, self.secrets, self.store_kind)
        return self._store


def handle_errors(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ConfigError as e:
            err.print(f"[red]Config error:[/red] {e}")
            raise typer.Exit(2) from e
        except KeyboardInterrupt:
            err.print("[yellow]Interrupted[/yellow]")
            raise typer.Exit(130) from None

    return wrapper  # type: ignore[return-value]


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", "-c", envvar="LEADGEN_CONFIG",
                                                help="Path to config.yaml [default: config/config.yaml]")] = None,
    store: Annotated[str | None, typer.Option("--store", help="Override the configured store: sheets | sqlite")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
) -> None:  # fmt: skip
    if store and store not in ("sheets", "sqlite"):
        raise typer.BadParameter("--store must be 'sheets' or 'sqlite'")
    ctx.obj = Ctx(config_path=resolve_config_path(config), store_kind=store, verbose=verbose)


# ============================================================================ setup
@app.command()
@handle_errors
def init(
    ctx: typer.Context, force: Annotated[bool, typer.Option(help="Overwrite existing files")] = False
) -> None:
    """Create config/config.yaml, .env, and data/logs folders in the current directory."""
    c: Ctx = ctx.obj
    data = resources.files("leadgen.data")
    targets = {c.config_path: "config.example.yaml", Path(".env"): "env.example"}
    for target, template in targets.items():
        if target.exists() and not force:
            console.print(f"[yellow]exists[/yellow] {target} (use --force to overwrite)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data.joinpath(template).read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"[green]created[/green] {target}")
    for d in ("data", "logs", "secrets", "exports"):
        Path(d).mkdir(exist_ok=True)
    console.print(
        "\nNext steps:\n"
        "  1. Edit [bold]config/config.yaml[/bold]: set user_agent to your bot name + contact, define searches\n"
        "  2. Fill in [bold].env[/bold]: GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_PLACES_API_KEY (optional)\n"
        "  3. [bold]leads doctor[/bold] → [bold]leads sheet init[/bold] → [bold]leads run <search> --dry-run[/bold]"
    )


@app.command()
@handle_errors
def doctor(ctx: typer.Context) -> None:
    """Check configuration, credentials, and connectivity."""
    c: Ctx = ctx.obj
    rows: list[tuple[str, str, str]] = []

    def add(check: str, ok: bool | None, detail: str) -> None:
        rows.append(
            (
                check,
                "[green]ok[/green]" if ok else ("[yellow]warn[/yellow]" if ok is None else "[red]FAIL[/red]"),
                detail,
            )
        )

    try:
        cfg = c.cfg
        add("config", True, str(c.config_path))
    except ConfigError as e:
        add("config", False, str(e))
        _print_checks(rows)
        raise typer.Exit(1) from e

    add(
        "user agent",
        None if cfg.user_agent_is_placeholder else True,
        "still the example value — set your own bot name/contact"
        if cfg.user_agent_is_placeholder
        else cfg.user_agent,
    )
    add(
        "searches",
        bool(cfg.searches) or None,
        f"{len(cfg.searches)} configured, {len(cfg.schedules)} schedules",
    )

    s = c.secrets
    store_kind = c.store_kind or cfg.store
    if store_kind == "sheets":
        sa = s.google_service_account_file
        if not sa or not sa.exists():
            add("service account", False, f"GOOGLE_SERVICE_ACCOUNT_FILE missing or not found: {sa}")
        else:
            email = json.loads(sa.read_text()).get("client_email", "?")
            add("service account", True, f"{email}  ← share the Google Sheet with this address (Editor)")
        if not s.google_sheet_id:
            add("google sheet", False, "GOOGLE_SHEET_ID not set")
        elif sa and sa.exists():
            try:
                store = c.store
                tabs = [ws.title for ws in store.sh.worksheets()]  # type: ignore[attr-defined]
                missing = [t for t in ("Leads", "Activities", "Suppression", "Runs") if t not in tabs]
                add(
                    "google sheet",
                    None if missing else True,
                    f"'{store.title}' tabs={tabs}" + (" → run `leads sheet init`" if missing else ""),
                )  # type: ignore[attr-defined]
            except Exception as e:
                add("google sheet", False, f"{type(e).__name__}: {e}")
    else:
        add("sqlite store", True, str(cfg.sqlite_path))

    uses_places = any("google_places" in x.sources for x in cfg.searches)
    if s.google_places_api_key:
        try:
            from leadgen.http.fetcher import PoliteFetcher
            from leadgen.sources.google_places import GooglePlacesSource

            async def _ping() -> None:
                async with PoliteFetcher(cfg.crawl, cfg.user_agent, cfg.user_agent_token) as f:
                    await GooglePlacesSource(f, s.google_places_api_key, cfg.google_places).ping()

            asyncio.run(_ping())
            add("google places", True, "API key works")
        except Exception as e:
            add("google places", False, str(e))
    else:
        add(
            "google places",
            False if uses_places else None,
            "GOOGLE_PLACES_API_KEY not set"
            + (" (used by a configured search)" if uses_places else " (optional)"),
        )

    headers = {"User-Agent": cfg.user_agent}
    for name, url in (("overpass", cfg.osm.overpass_url.replace("/interpreter", "/status")),
                      ("nominatim", cfg.osm.nominatim_url.replace("/search", "/status"))):  # fmt: skip
        try:
            r = httpx.get(url, headers=headers, timeout=10)
            add(name, r.status_code < 400, f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            add(name, False, str(e))

    try:
        from leadgen.validate.email import MxResult, dns_mx_lookup

        mx = asyncio.run(dns_mx_lookup("gmail.com"))
        add("dns (MX checks)", mx == MxResult.HAS_MX, mx.value)
    except Exception as e:
        add("dns (MX checks)", False, str(e))

    if cfg.crawl.render_js:
        try:
            import playwright  # noqa: F401

            add("playwright", True, "installed")
        except ImportError:
            add(
                "playwright",
                False,
                "render_js is on: uv sync --extra browser && uv run playwright install chromium",
            )
    _print_checks(rows)


def _print_checks(rows: list[tuple[str, str, str]]) -> None:
    t = Table(title="leads doctor", show_lines=False)
    t.add_column("check")
    t.add_column("status")
    t.add_column("detail", overflow="fold")
    for r in rows:
        t.add_row(*r)
    console.print(t)


@sheet_app.command("init")
@handle_errors
def sheet_init(ctx: typer.Context) -> None:
    """Create/upgrade tabs, headers, status dropdowns and the Summary tab in the Google Sheet."""
    c: Ctx = ctx.obj
    if (c.store_kind or c.cfg.store) != "sheets":
        raise ConfigError("`sheet init` needs store: sheets")
    c.store.setup()
    console.print(
        f"[green]Google Sheet ready:[/green] https://docs.google.com/spreadsheets/d/{c.secrets.google_sheet_id}"
    )


# ============================================================================ running
def _print_run(result: RunResult, dry_run: bool, out: Path | None) -> None:
    s = result.stats
    t = Table(title=f"{s.run_id}  ({s.search})" + ("  [dry-run]" if dry_run else ""))
    for col in ("discovered", "crawled", "new", "duplicates", "merged", "suppressed", "skipped", "errors"):
        t.add_column(col, justify="right")
    t.add_row(*(str(getattr(s, col)) for col in
                ("discovered", "crawled", "new", "duplicates", "merged", "suppressed", "skipped", "errors")))  # fmt: skip
    console.print(t)
    if s.notes:
        console.print(f"[yellow]notes:[/yellow] {s.notes}")
    if dry_run and result.leads:
        _print_leads(result.leads[:50], title="Leads that would be added (first 50)")
    if out and result.leads:
        _write_leads(result.leads, out, "csv")
        console.print(f"[green]wrote[/green] {len(result.leads)} leads to {out}")


@app.command()
@handle_errors
def run(
    ctx: typer.Context,
    search: Annotated[str | None, typer.Argument(help="Name of a search defined in config.yaml")] = None,
    all_searches: Annotated[bool, typer.Option("--all", help="Run every configured search")] = False,
    keyword: Annotated[list[str] | None, typer.Option("--keyword", "-k", help="Ad-hoc keyword (repeatable)")] = None,
    location: Annotated[list[str] | None, typer.Option("--location", "-l", help="Ad-hoc location (repeatable)")] = None,
    source: Annotated[list[str] | None, typer.Option("--source", "-s", help="osm | google_places (repeatable)")] = None,
    industry: Annotated[str | None, typer.Option(help="Force industry label for ad-hoc runs")] = None,
    osm_tag: Annotated[list[str] | None, typer.Option("--osm-tag", help='e.g. "office=lawyer" (repeatable)')] = None,
    max_results: Annotated[int, typer.Option(help="Ad-hoc: max results per keyword x location x source")] = 50,
    limit: Annotated[int | None, typer.Option(help="Stop after processing N new candidates")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Collect and show leads without writing")] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-crawl domains even if crawled recently")] = False,
    out: Annotated[Path | None, typer.Option(help="Also write the new leads to this CSV")] = None,
) -> None:  # fmt: skip
    """Discover and store leads for a configured search, all searches, or an ad-hoc keyword + location."""
    from leadgen.runner import run_search

    c: Ctx = ctx.obj
    modes = sum([bool(search), all_searches, bool(keyword or location)])
    if modes != 1:
        raise typer.BadParameter("give exactly one of: SEARCH, --all, or --keyword/--location")
    for src in source or []:
        if src not in KNOWN_SOURCES - {"seed"}:
            raise typer.BadParameter(f"unknown source '{src}'")

    if keyword or location:
        if not (keyword and location):
            raise typer.BadParameter("ad-hoc runs need both --keyword and --location")
        searches = [SearchConfig(name="adhoc", keywords=keyword, locations=location, sources=source or ["osm"],
                                 industry=industry, osm_tags=osm_tag or [], max_results=max_results)]  # fmt: skip
    elif all_searches:
        searches = c.cfg.searches
        if not searches:
            raise ConfigError("no searches defined in config")
    else:
        searches = [c.cfg.get_search(search)]  # type: ignore[arg-type]

    for s in searches:
        result = asyncio.run(run_search(
            c.cfg, c.secrets, c.store, s, sources=source if (source and not keyword) else None,
            limit=limit, dry_run=dry_run, force_recrawl=force,
        ))  # fmt: skip
        _print_run(result, dry_run, out)


@app.command("add-urls")
@handle_errors
def add_urls(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(exists=True, readable=True, help=".txt (one URL per line) or .csv with a website column")],
    location: Annotated[str, typer.Option(help='Fallback location, e.g. "Bristol, UK"')] = "",
    industry: Annotated[str | None, typer.Option(help="Industry label for all rows (CSV 'industry' column wins)")] = None,
    tag: Annotated[list[str] | None, typer.Option(help="Tag(s) to add")] = None,
    limit: Annotated[int | None, typer.Option()] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    out: Annotated[Path | None, typer.Option()] = None,
) -> None:  # fmt: skip
    """Crawl websites you already have and turn them into leads."""
    from leadgen.runner import run_seed_file

    c: Ctx = ctx.obj
    result = asyncio.run(run_seed_file(
        c.cfg, c.secrets, c.store, file, location=location, industry=industry, tags=tag or [],
        limit=limit, dry_run=dry_run, force_recrawl=force,
    ))  # fmt: skip
    _print_run(result, dry_run, out)


# ============================================================================ viewing / editing
def _filtered(c: Ctx, **filters: Any) -> list[Lead]:
    from leadgen.views import filter_leads

    leads = c.store.load_leads()
    return filter_leads(leads, **filters)


def _print_leads(leads: list[Lead], title: str = "Leads") -> None:
    t = Table(title=f"{title} ({len(leads)})", show_lines=False)
    for col in ("lead_id", "status", "company_name", "email", "email_status", "city", "country", "industry"):
        t.add_column(col, overflow="fold", max_width=34)
    for lead in leads:
        t.add_row(lead.lead_id, lead.status, lead.company_name, lead.email, lead.email_status,
                  lead.city, lead.country, lead.industry)  # fmt: skip
    console.print(t)


def _write_leads(leads: list[Lead], path: Path | None, fmt: str) -> None:
    rows = [lead.to_row() for lead in leads]
    if fmt == "json":
        text = json.dumps(rows, indent=2, ensure_ascii=False)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text + "\n")
        return
    fh = path.open("w", newline="", encoding="utf-8") if path else sys.stdout
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        w = csv.DictWriter(fh, fieldnames=LEAD_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    finally:
        if path:
            fh.close()


FilterIndustry = Annotated[list[str] | None, typer.Option("--industry", "-i")]
FilterCountry = Annotated[list[str] | None, typer.Option("--country")]
FilterCity = Annotated[list[str] | None, typer.Option("--city")]
FilterStatus = Annotated[list[str] | None, typer.Option("--status")]
FilterEmailStatus = Annotated[list[str] | None, typer.Option("--email-status")]
FilterSource = Annotated[list[str] | None, typer.Option("--source")]


@app.command("list")
@handle_errors
def list_leads(
    ctx: typer.Context,
    industry: FilterIndustry = None,
    country: FilterCountry = None,
    city: FilterCity = None,
    status: FilterStatus = None,
    email_status: FilterEmailStatus = None,
    source: FilterSource = None,
    search: Annotated[str | None, typer.Option("--search", "-q", help="Text search in name/email/domain/notes")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="table | csv | json")] = "table",
    limit: Annotated[int, typer.Option(help="Max rows (table format)")] = 100,
    group_by: Annotated[str | None, typer.Option(help="Show counts by a field instead, e.g. industry, country, status")] = None,
) -> None:  # fmt: skip
    """List leads, filtered by industry / location / status."""
    from leadgen.views import count_by

    c: Ctx = ctx.obj
    leads = _filtered(c, industry=industry, country=country, city=city, status=status,
                      email_status=email_status, source=source, text=search)  # fmt: skip
    leads.sort(key=lambda lead: lead.created_at, reverse=True)
    if group_by:
        if group_by not in LEAD_COLUMNS:
            raise typer.BadParameter(f"unknown field '{group_by}'")
        t = Table(title=f"Leads by {group_by}")
        t.add_column(group_by)
        t.add_column("leads", justify="right")
        for value, n in count_by(leads, group_by):
            t.add_row(value, str(n))
        console.print(t)
    elif fmt == "table":
        _print_leads(leads[:limit])
    elif fmt in ("csv", "json"):
        _write_leads(leads, None, fmt)
    else:
        raise typer.BadParameter("--format must be table, csv, or json")


@app.command()
@handle_errors
def status(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help="lead_id, email, or domain")],
    new_status: Annotated[str, typer.Argument(help=" | ".join(s.value for s in LeadStatus))],
    note: Annotated[str | None, typer.Option(help="Append a note")] = None,
) -> None:
    """Change a lead's pipeline status (logged to Activities)."""
    from leadgen.views import find_lead

    c: Ctx = ctx.obj
    if new_status not in {s.value for s in LeadStatus}:
        raise typer.BadParameter(f"status must be one of: {', '.join(s.value for s in LeadStatus)}")
    lead = find_lead(c.store.load_leads(), identifier)
    if not lead:
        err.print(f"[red]No lead matches[/red] {identifier}")
        raise typer.Exit(1)
    fields = {"status": new_status}
    if new_status == LeadStatus.CONTACTED:
        fields["last_contacted_at"] = utcnow_iso()
    if note:
        fields["notes"] = (lead.notes + "\n" if lead.notes else "") + f"[{datetime.now(UTC):%Y-%m-%d}] {note}"
    c.store.update_leads({lead.lead_id: fields})
    c.store.add_activity(
        lead.lead_id, "status_change", f"{lead.status} -> {new_status}" + (f": {note}" if note else "")
    )
    console.print(f"[green]{lead.company_name}[/green]: {lead.status} → {new_status}")


@app.command()
@handle_errors
def validate(
    ctx: typer.Context,
    recheck: Annotated[bool, typer.Option("--recheck", help="Re-check every email, not just risky/unchecked ones")] = False,
) -> None:  # fmt: skip
    """Re-validate stored email addresses (DNS/MX, disposable, role/free-mail)."""
    from leadgen.pipeline import revalidate
    from leadgen.validate.email import EmailValidator

    c: Ctx = ctx.obj
    n = asyncio.run(revalidate(c.store, EmailValidator(), recheck_all=recheck))
    console.print(f"[green]updated[/green] {n} leads")


@app.command()
@handle_errors
def export(
    ctx: typer.Context,
    out: Annotated[Path | None, typer.Option(help="Output file [default: exports/leads_<timestamp>.<fmt>]")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="csv | json")] = "csv",
    industry: FilterIndustry = None,
    country: FilterCountry = None,
    city: FilterCity = None,
    status: FilterStatus = None,
    email_status: FilterEmailStatus = None,
    include_opted_out: Annotated[bool, typer.Option(help="Include unsubscribed/do_not_contact/bounced leads")] = False,
) -> None:  # fmt: skip
    """Export leads (e.g. for a cold-email tool). Opted-out and invalid-email leads are excluded by default."""
    c: Ctx = ctx.obj
    if fmt not in ("csv", "json"):
        raise typer.BadParameter("--format must be csv or json")
    leads = _filtered(c, industry=industry, country=country, city=city, status=status,
                      email_status=email_status, contactable_only=not include_opted_out)  # fmt: skip
    path = out or Path("exports") / f"leads_{datetime.now():%Y%m%d_%H%M%S}.{fmt}"
    _write_leads(leads, path, fmt)
    console.print(f"[green]exported[/green] {len(leads)} leads to {path}")


# ============================================================================ privacy
@app.command()
@handle_errors
def forget(
    ctx: typer.Context,
    identifier: Annotated[str, typer.Argument(help="Email address (one person) or domain (whole company)")],
    reason: Annotated[str, typer.Option(help="Recorded with the suppression entry")] = "erasure request",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:  # fmt: skip
    """GDPR erasure: delete matching data and block it from being collected again."""
    from leadgen.compliance import forget as do_forget
    from leadgen.compliance import parse_identifier
    from leadgen.store.state import CrawlState

    c: Ctx = ctx.obj
    kind, value = parse_identifier(identifier)
    if not yes:
        typer.confirm(f"Erase all data for {kind} '{value}' and suppress it permanently?", abort=True)
    result = do_forget(c.store, identifier, reason=reason)
    if kind == "domain":
        state = CrawlState(c.cfg.state_path)
        state.forget_domain(value)
        state.close()
    console.print(
        f"[green]done[/green]: {result.leads_deleted} leads deleted, {result.emails_scrubbed} leads scrubbed, "
        f"{kind} '{value}' added to suppression list"
    )


@app.command()
@handle_errors
def purge(
    ctx: typer.Context,
    older_than: Annotated[int | None, typer.Option(help="Days since last update [default: compliance.retention_days]")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:  # fmt: skip
    """Retention clean-up: delete leads with no engagement older than the retention period."""
    from leadgen.compliance import purge as do_purge

    c: Ctx = ctx.obj
    days = older_than or c.cfg.compliance.retention_days
    preview = do_purge(c.store, days, dry_run=True)
    console.print(f"{preview.deleted} leads older than {days} days are eligible "
                  f"({preview.suppressed} opt-outs will be kept as suppression hashes)")  # fmt: skip
    if dry_run or not preview.deleted:
        return
    if not yes:
        typer.confirm("Delete them now?", abort=True)
    result = do_purge(c.store, days)
    console.print(f"[green]purged[/green] {result.deleted} leads")


# ============================================================================ scheduler / dashboard
@scheduler_app.command("start")
@handle_errors
def scheduler_start(ctx: typer.Context) -> None:
    """Run configured schedules in the foreground (use launchd/systemd/tmux to keep it alive)."""
    from leadgen.scheduler import start

    c: Ctx = ctx.obj
    _ = c.cfg  # validate config + set up logging first
    start(c.config_path, c.store_kind)


@scheduler_app.command("list")
@handle_errors
def scheduler_list(ctx: typer.Context) -> None:
    """Show configured schedules and their next run time."""
    from leadgen.scheduler import upcoming

    c: Ctx = ctx.obj
    t = Table(title=f"Schedules ({c.cfg.timezone})")
    for col in ("search", "cron", "next run"):
        t.add_column(col)
    for search, cron, nxt in upcoming(c.cfg):
        t.add_row(search, cron, f"{nxt:%Y-%m-%d %H:%M %Z}" if nxt else "-")
    console.print(t)


@app.command()
@handle_errors
def dashboard(
    ctx: typer.Context,
    port: Annotated[int, typer.Option(help="Port for the local dashboard")] = 8501,
) -> None:
    """Open the Streamlit dashboard (needs: uv sync --extra dashboard)."""
    c: Ctx = ctx.obj
    try:
        import streamlit  # noqa: F401
    except ImportError as e:
        raise ConfigError("dashboard needs the extra: uv sync --extra dashboard") from e
    app_path = Path(__file__).parent / "dashboard" / "app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_path), "--server.port", str(port),
           "--server.address", "localhost", "--browser.gatherUsageStats", "false", "--",
           "--config", str(c.config_path.resolve())]  # fmt: skip
    if c.store_kind:
        cmd += ["--store", c.store_kind]
    raise typer.Exit(subprocess.call(cmd))


if __name__ == "__main__":  # pragma: no cover
    app()
