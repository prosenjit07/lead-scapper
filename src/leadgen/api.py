"""REST API for leads, runs and privacy requests: FastAPI, documented with OpenAPI.

Swagger UI: /docs · ReDoc: /redoc · schema: /openapi.json. Start it with `leads api` (needs the
`api` extra). It reuses the same store, filters and compliance code as the CLI, so the CLI, dashboard,
scheduler and API all see the same data and follow the same rules.
"""

import asyncio
import csv
import io
import json
import secrets as pysecrets
import threading
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from leadgen import __version__
from leadgen.config import AppConfig, ConfigError, SearchConfig, Secrets
from leadgen.logging_setup import get_logger
from leadgen.models import LEAD_COLUMNS, RUN_COLUMNS, EmailStatus, Lead, LeadStatus, RunStats, utcnow_iso
from leadgen.runner import run_search
from leadgen.store import open_store
from leadgen.store.base import LeadStore
from leadgen.views import append_note, export_filename, filter_leads, find_lead, status_change

log = get_logger("api")

DESCRIPTION = """
REST access to the leads collected by **leadgen**: query and update leads, log CRM activity, start
configured searches, export contactable leads, and handle GDPR erasure requests.

**Base path `/api/v1`.** Breaking changes will get a new prefix (`/api/v2`) while `/api/v1` keeps working,
so integrations don't break on upgrade. `/health` is unversioned, for uptime monitors and load balancers.

**Authentication.** If `LEADGEN_API_KEY` is set in `.env`, every endpoint except `/health` requires the
header `X-API-Key: <key>` (use **Authorize** above). Without a key the server only listens on localhost.

**Data.** One lead per company. All lead fields are strings, and `""` means unknown, because the
default store is a Google Sheet. `email_status=valid` means the domain accepts mail and matches the
business, not that the mailbox was confirmed.

**Compliance.** Skip leads whose status is `unsubscribed`, `do_not_contact` or `bounced` (the
`/api/v1/leads/export` endpoint already does), and say in your first email where you got the address and
how to opt out (GDPR Art. 14). Use `POST /api/v1/privacy/erasure` for erasure requests.
""".strip()

TAGS = [
    {"name": "leads", "description": "Query and update leads (one row per company)."},
    {
        "name": "activities",
        "description": "CRM events per lead: emails sent, replies, calls, status changes.",
    },
    {"name": "runs", "description": "Start configured searches and check their progress and history."},
    {"name": "export", "description": "Download contactable leads for an email tool."},
    {"name": "privacy", "description": "GDPR erasure (Art. 17) with permanent suppression."},
    {"name": "system", "description": "Health check."},
]

RUN_COUNT_FIELDS = ("discovered", "crawled", "new", "duplicates", "merged", "suppressed", "skipped", "errors")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_JOBS_KEPT = 100
API_PREFIX = "/api/v1"


# ============================================================================ schemas
class LeadPage(BaseModel):
    total: int = Field(description="Number of leads matching the filters")
    limit: int
    offset: int
    items: list[Lead]


def _check_iso(value: str | None) -> str | None:
    if value:
        try:
            datetime.fromisoformat(value)
        except ValueError as e:
            raise ValueError(
                "must be an ISO 8601 date or timestamp, e.g. 2026-11-17 or 2026-11-17T09:00:00Z"
            ) from e
    return value


IsoDate = Annotated[str | None, AfterValidator(_check_iso)]


class LeadUpdate(BaseModel):
    """Fields a CRM or email tool may change. Fields you leave out are not touched; `null` or `""`
    clears a field. Data found by the crawler (company, email, phone …) is read-only here."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"status": "contacted", "campaign": "q1-dental", "sequence_step": "1",
                                         "append_note": "intro email sent"}]},
    )  # fmt: skip

    status: LeadStatus | None = Field(
        None,
        description="New pipeline status. A change is logged to Activities; `contacted` also sets "
        "`last_contacted_at` unless you send it.",
    )
    append_note: str | None = Field(None, description="Appended to `notes` with today's date")
    notes: str | None = Field(None, description="Replaces `notes` entirely")
    owner: str | None = None
    tags: str | None = Field(None, description="Comma separated")
    campaign: str | None = None
    sequence_step: str | None = None
    last_contacted_at: IsoDate = Field(None, description="ISO 8601")
    next_follow_up_at: IsoDate = Field(None, description="ISO 8601")


class Activity(BaseModel):
    lead_id: str
    timestamp: str = Field(description="ISO 8601 UTC")
    type: str
    details: str = ""
    campaign: str = ""


class ActivityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(
        pattern=r"^[a-z][a-z0-9_]{0,39}$",
        description="Event type, e.g. `email_sent`, `reply`, `call`, `meeting`, `bounce`",
        examples=["email_sent"],
    )
    details: str = Field("", max_length=2000, examples=["Intro email, sequence step 1"])
    campaign: str = Field("", max_length=200, examples=["q1-dental"])


class RunRecord(BaseModel):
    """A finished run, as logged in the Runs tab / table."""

    run_id: str
    started_at: str
    finished_at: str = ""
    search: str = ""
    sources: str = Field("", description="Comma separated")
    discovered: int = 0
    crawled: int = 0
    new: int = Field(0, description="Leads added")
    duplicates: int = 0
    merged: int = Field(0, description="Existing leads whose blank fields were filled")
    suppressed: int = 0
    skipped: int = 0
    errors: int = 0
    notes: str = Field("", description="Error notes")

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RunRecord":
        data: dict[str, Any] = {c: row.get(c) or "" for c in RUN_COLUMNS}
        for f in RUN_COUNT_FIELDS:
            try:
                data[f] = int(data[f] or 0)
            except (TypeError, ValueError):
                data[f] = 0
        return cls(**data)

    @classmethod
    def from_stats(cls, stats: RunStats) -> "RunRecord":
        return cls.from_row(stats.to_row())


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(BaseModel):
    """A run started through the API. Jobs live in memory and are lost when the server restarts;
    finished runs are also logged in `GET /api/v1/runs`."""

    job_id: str
    search: str
    sources: list[str]
    dry_run: bool
    status: JobStatus
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    error: str = Field("", description="Set when status is `failed`")
    stats: RunRecord | None = Field(None, description="Set when the run finished")
    leads: list[Lead] = Field(
        default_factory=list, description="Dry runs only: the leads that would be added"
    )


class RunRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"search": "dental-uk", "dry_run": True, "limit": 10}]},
    )

    search: str = Field(description="Name of a search in config.yaml (see `GET /api/v1/searches`)")
    sources: list[Literal["osm", "google_places"]] | None = Field(
        None, description="Override the search's sources"
    )
    limit: int | None = Field(None, ge=1, description="Stop after processing this many new businesses")
    dry_run: bool = Field(
        False, description="Collect leads without saving them; they are returned in the job"
    )
    force: bool = Field(False, description="Re-crawl websites even if crawled within `recrawl_after_days`")


class SearchInfo(SearchConfig):
    schedules: list[str] = Field(default_factory=list, description="Cron expressions (config timezone)")
    next_run: str | None = Field(
        None, description="Next scheduled start (ISO 8601). Only fires while `leads scheduler start` runs."
    )


class ForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(
        description="An email address (erases that person) or a domain (erases the whole company)",
        examples=["jane@acme.com", "acme.com"],
    )
    reason: str = Field("erasure request", max_length=200)


class ForgetResult(BaseModel):
    kind: Literal["email", "domain"]
    value: str
    leads_deleted: int
    emails_scrubbed: int = Field(description="Leads kept but with the email (and its contact) removed")


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    store: str


# ============================================================================ state + dependencies
class ApiState:
    def __init__(
        self, cfg: AppConfig, secrets: Secrets, store_kind: str | None = None, store: LeadStore | None = None
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.store_kind = store_kind or cfg.store
        self._store = store
        # SQLite connections and the Sheets client are not safe for concurrent use, so requests take
        # turns. PostgreSQL uses a connection pool, so requests run in parallel. Background runs open
        # their own store either way.
        self.store_lock: AbstractContextManager[Any] = (
            nullcontext() if self.store_kind == "postgres" else threading.Lock()
        )
        self._open_lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self.jobs_lock = threading.Lock()

    @property
    def store(self) -> LeadStore:
        if self._store is None:  # opened lazily so /health and /docs work without credentials
            with self._open_lock:  # two concurrent first requests must not open two stores
                if self._store is None:
                    self._store = open_store(self.cfg, self.secrets, self.store_kind)
        return self._store


def get_state(request: Request) -> ApiState:
    return request.app.state.leadgen


StateDep = Annotated[ApiState, Depends(get_state)]


def locked_store(state: StateDep) -> Iterator[LeadStore]:
    with state.store_lock:
        yield state.store


StoreDep = Annotated[LeadStore, Depends(locked_store)]

api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Required when LEADGEN_API_KEY is set on the server"
)


def require_api_key(state: StateDep, key: Annotated[str | None, Security(api_key_header)] = None) -> None:
    expected = state.secrets.leadgen_api_key
    if expected and not (key and pysecrets.compare_digest(key.encode(), expected.encode())):
        raise HTTPException(
            401, "missing or invalid X-API-Key header", headers={"WWW-Authenticate": "APIKey"}
        )


def lead_filters(
    industry: Annotated[list[str] | None, Query(description="Exact industry label, case-insensitive (repeatable)")] = None,
    country: Annotated[list[str] | None, Query(description="e.g. `United Kingdom` (repeatable)")] = None,
    city: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    status: Annotated[list[LeadStatus] | None, Query(description="Repeatable")] = None,
    email_status: Annotated[list[EmailStatus] | None, Query(description="Repeatable")] = None,
    source: Annotated[list[Literal["osm", "google_places", "seed"]] | None, Query(description="Repeatable")] = None,
    q: Annotated[str | None, Query(description="Text search in company, email, domain, contact, notes, tags")] = None,
) -> dict[str, Any]:  # fmt: skip
    return {"industry": industry, "country": country, "city": city, "status": status,
            "email_status": email_status, "source": source, "text": q}  # fmt: skip


FiltersDep = Annotated[dict[str, Any], Depends(lead_filters)]
LeadRef = Annotated[
    str, Path(description="The lead's `lead_id`, primary email, or domain", examples=["acme.com"])
]


def _get_lead(store: LeadStore, identifier: str) -> Lead:
    lead = find_lead(store.load_leads(), identifier)
    if lead is None:
        raise HTTPException(404, f"no lead matches '{identifier}'")
    return lead


NOT_FOUND = {404: {"description": "No lead matches the lead_id, email or domain"}}


# ============================================================================ routes
leads_router = APIRouter(prefix=f"{API_PREFIX}/leads", tags=["leads"])
activities_router = APIRouter(prefix=f"{API_PREFIX}/leads", tags=["activities"])
runs_router = APIRouter(prefix=API_PREFIX, tags=["runs"])
# /leads/export must be registered before /leads/{lead_ref}, or "export" would be taken as a lead_ref.
export_router = APIRouter(prefix=f"{API_PREFIX}/leads", tags=["export"])
privacy_router = APIRouter(prefix=f"{API_PREFIX}/privacy", tags=["privacy"])
system_router = APIRouter(tags=["system"])


@leads_router.get("", summary="List leads")
def list_leads(
    store: StoreDep,
    filters: FiltersDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeadPage:
    """Leads matching all the given filters, newest first. Repeat a parameter to match any of several
    values, e.g. `?city=Bristol&city=Leeds`."""
    leads = filter_leads(store.load_leads(), **filters)
    leads.sort(key=lambda lead: lead.created_at, reverse=True)
    return LeadPage(total=len(leads), limit=limit, offset=offset, items=leads[offset : offset + limit])


@leads_router.get("/{lead_ref}", summary="Get one lead", responses=NOT_FOUND)
def get_lead(store: StoreDep, lead_ref: LeadRef) -> Lead:
    """Look a lead up by its `lead_id`, its primary email, or its domain."""
    return _get_lead(store, lead_ref)


@leads_router.patch("/{lead_ref}", summary="Update a lead's CRM fields", responses=NOT_FOUND)
def update_lead(store: StoreDep, lead_ref: LeadRef, body: LeadUpdate) -> Lead:
    """Change pipeline status, owner, notes, campaign, follow-up dates and so on. A status change is
    logged to Activities, as with `leads status`."""
    if body.notes is not None and body.append_note:
        raise HTTPException(422, "send either `notes` (replace) or `append_note`, not both")
    lead = _get_lead(store, lead_ref)
    sent = body.model_dump(exclude_unset=True, exclude={"status", "append_note"})
    fields: dict[str, str] = {k: v or "" for k, v in sent.items()}
    activity = None
    if body.status is not None and body.status.value != lead.status:
        status_fields, activity = status_change(lead, body.status.value, body.append_note)
        fields = {**status_fields, **fields}  # explicitly sent values (e.g. last_contacted_at) win
    elif body.append_note:
        fields["notes"] = append_note(lead.notes, body.append_note)
    if fields:
        store.update_leads({lead.lead_id: fields})
    if activity:
        store.add_activity(lead.lead_id, "status_change", activity)
    return _get_lead(store, lead.lead_id)


@activities_router.get("/{lead_ref}/activities", summary="List a lead's activities", responses=NOT_FOUND)
def list_lead_activities(store: StoreDep, lead_ref: LeadRef) -> list[Activity]:
    """Oldest first."""
    lead = _get_lead(store, lead_ref)
    return [Activity(**row) for row in store.list_activities(lead.lead_id)]


@activities_router.post(
    "/{lead_ref}/activities", status_code=201, summary="Log an activity", responses=NOT_FOUND
)
def create_lead_activity(store: StoreDep, lead_ref: LeadRef, body: ActivityCreate) -> Activity:
    """Record a CRM event such as `email_sent` or `reply`. This does not change the lead; send a
    `PATCH /api/v1/leads/{lead_ref}` too if the status or `last_contacted_at` should change."""
    lead = _get_lead(store, lead_ref)
    store.add_activity(lead.lead_id, body.type, body.details, body.campaign)
    return Activity(**store.list_activities(lead.lead_id)[-1])


@runs_router.get("/searches", summary="List configured searches")
def list_searches(state: StateDep) -> list[SearchInfo]:
    """Searches defined in `config.yaml`, with their schedules. Restart the API after editing the config."""
    from leadgen.scheduler import upcoming

    by_search: dict[str, list[tuple[str, datetime | None]]] = {}
    for name, cron, nxt in upcoming(state.cfg):
        for s in state.cfg.searches if name == "all" else [state.cfg.get_search(name)]:
            by_search.setdefault(s.name, []).append((cron, nxt))
    out = []
    for s in state.cfg.searches:
        sched = by_search.get(s.name, [])
        nexts = [n for _, n in sched if n]
        out.append(SearchInfo(**s.model_dump(), schedules=[c for c, _ in sched],
                              next_run=min(nexts).isoformat() if nexts else None))  # fmt: skip
    return out


@runs_router.get("/runs", summary="Run history")
def list_runs(store: StoreDep, limit: Annotated[int, Query(ge=1, le=500)] = 20) -> list[RunRecord]:
    """Finished runs (from the CLI, scheduler, dashboard or API), newest first."""
    return [RunRecord.from_row(row) for row in store.list_runs(limit)]


@runs_router.post(
    "/runs",
    status_code=202,
    summary="Start a configured search",
    responses={
        404: {"description": "Unknown search"},
        409: {"description": "That search is already running"},
    },
)
def start_run(state: StateDep, body: RunRequest, response: Response) -> Job:
    """Runs in the background, since a search can take several minutes (websites are crawled politely).
    Poll `GET /api/v1/jobs/{job_id}` until `status` is `succeeded` or `failed`."""
    try:
        search = state.cfg.get_search(body.search)
    except ConfigError as e:
        raise HTTPException(404, str(e)) from e
    sources = list(body.sources or search.sources)
    with state.jobs_lock:
        if any(j.search == search.name and j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
               for j in state.jobs.values()):  # fmt: skip
            raise HTTPException(409, f"search '{search.name}' is already running")
        job = Job(job_id=f"job_{uuid.uuid4().hex[:12]}", search=search.name, sources=sources,
                  dry_run=body.dry_run, status=JobStatus.QUEUED, created_at=utcnow_iso())  # fmt: skip
        state.jobs[job.job_id] = job
        for old in list(state.jobs)[:-MAX_JOBS_KEPT]:
            if state.jobs[old].status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                del state.jobs[old]
    threading.Thread(
        target=_run_job, args=(state, job.job_id, search, body, sources), name=job.job_id, daemon=True
    ).start()
    response.headers["Location"] = f"{API_PREFIX}/jobs/{job.job_id}"
    return job.model_copy()


def _set_job(state: ApiState, job_id: str, **changes: Any) -> None:
    with state.jobs_lock:
        job = state.jobs[job_id]
        state.jobs[job_id] = job.model_copy(update=changes)


def _run_job(
    state: ApiState, job_id: str, search: SearchConfig, body: RunRequest, sources: list[str]
) -> None:
    _set_job(state, job_id, status=JobStatus.RUNNING, started_at=utcnow_iso())
    store: LeadStore | None = None
    try:
        store = open_store(state.cfg, state.secrets, state.store_kind)  # own connection/client
        result = asyncio.run(run_search(state.cfg, state.secrets, store, search, sources=sources,
                                        limit=body.limit, dry_run=body.dry_run, force_recrawl=body.force))  # fmt: skip
        _set_job(state, job_id, status=JobStatus.SUCCEEDED, stats=RunRecord.from_stats(result.stats),
                 leads=result.leads if body.dry_run else [], finished_at=utcnow_iso())  # fmt: skip
    except Exception as e:
        log.exception(f"API run {job_id} ({search.name}) failed")
        _set_job(state, job_id, status=JobStatus.FAILED, error=f"{type(e).__name__}: {e}",
                 finished_at=utcnow_iso())  # fmt: skip
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()


@runs_router.get("/jobs", summary="List API-started runs")
def list_jobs(state: StateDep) -> list[Job]:
    """Runs started through this API since the server started, newest first (without dry-run leads)."""
    with state.jobs_lock:
        jobs = [j.model_copy(update={"leads": []}) for j in state.jobs.values()]
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)


@runs_router.get(
    "/jobs/{job_id}", summary="Get a run's progress", responses={404: {"description": "Unknown job"}}
)
def get_job(state: StateDep, job_id: str) -> Job:
    with state.jobs_lock:
        job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job '{job_id}' (jobs are kept in memory until the server restarts)")
    return job


@export_router.get(
    "/export",
    summary="Export leads as CSV or JSON",
    response_class=Response,
    responses={200: {"description": "A file download (all lead fields)",
                     "content": {"text/csv": {"schema": {"type": "string"}},
                                 "application/json": {"schema": {"type": "array",
                                                                 "items": {"$ref": "#/components/schemas/Lead"}}}}}},
)  # fmt: skip
def export_leads(
    store: StoreDep,
    filters: FiltersDep,
    format: Annotated[Literal["csv", "json"], Query(description="File format")] = "csv",
    include_opted_out: Annotated[
        bool, Query(description="Include leads without an email, with an invalid email, or opted out")
    ] = False,
) -> Response:
    """For an email tool. By default only *contactable* leads are included: they have an email that
    isn't `invalid`, and their status isn't `unsubscribed`, `do_not_contact` or `bounced`."""
    leads = filter_leads(store.load_leads(), **filters, contactable_only=not include_opted_out)
    leads.sort(key=lambda lead: lead.created_at, reverse=True)
    rows = [lead.to_row() for lead in leads]
    if format == "json":
        body, media = json.dumps(rows, ensure_ascii=False, indent=2), "application/json"
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=LEAD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        body, media = buf.getvalue(), "text/csv; charset=utf-8"
    headers = {
        "Content-Disposition": f'attachment; filename="{export_filename(filters, format)}"',
        "X-Total-Count": str(len(rows)),
    }
    return Response(body, media_type=media, headers=headers)


@privacy_router.post("/erasure", summary="Erase a person or company (GDPR Art. 17 right to erasure)")
def request_erasure(state: StateDep, store: StoreDep, body: ForgetRequest) -> ForgetResult:
    """**Irreversible.** An email removes that address everywhere (and deletes the lead if it was the
    only way to contact it). A domain deletes every lead for that company. Either way a SHA-256 hash is
    added to the suppression list, so it is never collected or contacted again."""
    from leadgen.compliance import forget as do_forget
    from leadgen.compliance import parse_identifier
    from leadgen.store.state import CrawlState

    try:
        kind, value = parse_identifier(body.identifier)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    result = do_forget(store, body.identifier, reason=body.reason)
    if kind == "domain":
        crawl_state = CrawlState(state.cfg.state_path)
        try:
            crawl_state.forget_domain(value)
        finally:
            crawl_state.close()
    return ForgetResult(kind=result.kind, value=result.value, leads_deleted=result.leads_deleted,  # type: ignore[arg-type]
                        emails_scrubbed=result.emails_scrubbed)  # fmt: skip


@system_router.get("/health", summary="Health check")
def get_health(state: StateDep) -> Health:
    """No authentication and no store access."""
    return Health(version=__version__, store=state.store_kind)


# ============================================================================ app
def create_app(
    cfg: AppConfig, secrets: Secrets, store_kind: str | None = None, store: LeadStore | None = None
) -> FastAPI:
    app = FastAPI(
        title="leadgen API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        # operationId = the function name (list_leads, export_leads …) instead of FastAPI's
        # list_leads_api_v1_leads_get; client generators turn these into method names.
        generate_unique_id_function=lambda route: route.name,
        license_info={"name": "Data from OpenStreetMap is © OpenStreetMap contributors (ODbL)",
                      "url": "https://www.openstreetmap.org/copyright"},
    )  # fmt: skip
    app.state.leadgen = ApiState(cfg, secrets, store_kind, store)
    auth = [Depends(require_api_key)]
    for router in (export_router, leads_router, activities_router, runs_router, privacy_router):
        app.include_router(router, dependencies=auth)
    app.include_router(system_router)

    @app.exception_handler(ConfigError)
    def _config_error(_: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    return app


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


class _SpecDumper(yaml.SafeDumper):
    """Readable YAML: multi-line text as `|` blocks, no &anchors for repeated schema objects."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


_SpecDumper.add_representer(
    str,
    lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None),
)


def openapi_text(app: FastAPI, fmt: Literal["json", "yaml"] = "json") -> str:
    """The app's OpenAPI (Swagger) document as JSON or YAML text."""
    spec = app.openapi()
    if fmt == "yaml":
        return yaml.dump(spec, Dumper=_SpecDumper, sort_keys=False, allow_unicode=True, width=110)
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
