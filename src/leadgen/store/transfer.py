"""Copy everything from one store into PostgreSQL (`leads db import`).

Safe to re-run: rows already in the destination are skipped, and activities are matched on all their
fields so they aren't duplicated. Before writing anything, every lead is checked against the PostgreSQL
constraints; if any value would be rejected, nothing is written and the problems are reported."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from leadgen.models import EmailStatus, EmailType, Lead, LeadStatus, RunStats
from leadgen.store.base import LeadStore
from leadgen.store.postgres_store import LEAD_TIMESTAMPS, RUN_INT_FIELDS, PostgresStore, to_ts

VALID = {
    "status": {s.value for s in LeadStatus},
    "email_status": {"", *(s.value for s in EmailStatus)},
    "email_type": {"", *(t.value for t in EmailType)},
}
ALL_ROWS = 10**9


@dataclass
class TableCount:
    source: int = 0
    copied: int = 0
    skipped: int = 0  # already present, duplicate, or orphaned


@dataclass
class TransferReport:
    dry_run: bool
    tables: dict[str, TableCount] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # blocking: nothing was written
    notes: list[str] = field(default_factory=list)  # informational: e.g. which duplicates were skipped

    @property
    def ok(self) -> bool:
        return not self.problems


def _check_lead(lead: Lead) -> list[str]:
    problems = [
        f"lead {lead.lead_id}: {name} {getattr(lead, name)!r} is not one of {sorted(VALID[name])}"
        for name in VALID
        if getattr(lead, name) not in VALID[name]
    ]
    for name in sorted(LEAD_TIMESTAMPS):
        try:
            to_ts(getattr(lead, name), name)
        except ValueError as e:
            problems.append(f"lead {lead.lead_id}: {e}")
    if not lead.lead_id:
        problems.append(f"lead without lead_id: {lead.company_name or lead.domain or '?'}")
    return problems


def _drop_duplicates(leads: list[Lead], notes: list[str]) -> list[Lead]:
    """PostgreSQL allows one lead per domain and per email; keep the oldest, report the rest."""
    kept: list[Lead] = []
    owner: dict[str, str] = {}
    for lead in sorted(leads, key=lambda x: (x.created_at or "~", x.lead_id)):
        keys = [
            f"domain {lead.domain}" if lead.domain else "",
            f"email {lead.email.lower()}" if lead.email else "",
        ]
        clash = next((k for k in keys if k and k in owner), None)
        if clash:
            notes.append(f"skipped {lead.lead_id} ({lead.company_name}): same {clash} as {owner[clash]}")
            continue
        for k in keys:
            if k:
                owner[k] = lead.lead_id
        kept.append(lead)
    return kept


def _activity_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row.get("lead_id", ""), to_ts(row.get("timestamp")), row.get("type", ""),
            row.get("details", ""), row.get("campaign", ""))  # fmt: skip


def copy_store(src: LeadStore, dst: PostgresStore, *, dry_run: bool = False) -> TransferReport:
    report = TransferReport(dry_run=dry_run)

    leads = src.load_leads()
    for lead in leads:
        report.problems.extend(_check_lead(lead))
    suppression = src.list_suppression()
    runs = src.list_runs(ALL_ROWS)
    activities = src.list_activities(None)
    for row in runs:
        try:
            to_ts(row.get("started_at"), "started_at")
            to_ts(row.get("finished_at"), "finished_at")
        except ValueError as e:
            report.problems.append(f"run {row.get('run_id')}: {e}")
    for row in activities:
        try:
            to_ts(row.get("timestamp"), "timestamp")
        except ValueError as e:
            report.problems.append(f"activity for {row.get('lead_id')}: {e}")
    if report.problems:
        return report

    unique = _drop_duplicates(leads, report.notes)
    report.tables["leads"] = TableCount(source=len(leads))
    report.tables["suppression"] = TableCount(source=len(suppression))
    report.tables["runs"] = TableCount(source=len(runs))
    report.tables["activities"] = TableCount(source=len(activities))
    if dry_run:
        report.tables["leads"].skipped = len(leads) - len(unique)
        return report

    # leads: ON CONFLICT DO NOTHING, so re-running copies nothing twice
    copied = dst.add_leads(unique)
    report.tables["leads"].copied, report.tables["leads"].skipped = copied, len(leads) - copied

    before = len(dst.load_suppression())
    dst.add_suppression(suppression)
    report.tables["suppression"].copied = len(dst.load_suppression()) - before
    report.tables["suppression"].skipped = len(suppression) - report.tables["suppression"].copied

    before = len(dst.list_runs(ALL_ROWS))
    for row in runs:
        ints = {f: int(row.get(f) or 0) for f in RUN_INT_FIELDS}
        dst.log_run(RunStats(**{**{k: row.get(k, "") or "" for k in ("run_id", "started_at", "finished_at",
                                                                       "search", "sources", "notes")}, **ints}))  # fmt: skip
    report.tables["runs"].copied = len(dst.list_runs(ALL_ROWS)) - before
    report.tables["runs"].skipped = len(runs) - report.tables["runs"].copied

    # activities: only for leads that exist in the destination, and only ones not already copied
    lead_ids = {lead.lead_id for lead in dst.load_leads()}
    existing = {_activity_key(r) for r in dst.list_activities(None)}
    new_rows = [r for r in activities if r.get("lead_id") in lead_ids and _activity_key(r) not in existing]
    orphans = sum(1 for r in activities if r.get("lead_id") not in lead_ids)
    if orphans:
        report.notes.append(f"skipped {orphans} activities whose lead doesn't exist (deleted or skipped)")
    dst.add_activities(new_rows)
    report.tables["activities"].copied = len(new_rows)
    report.tables["activities"].skipped = len(activities) - len(new_rows)
    return report
