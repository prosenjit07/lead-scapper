"""The same behaviour from every LeadStore backend: SQLite, Google Sheets (fake gspread) and PostgreSQL
(only when TEST_DATABASE_URL is set). Anything the CLI, API, dashboard or pipeline relies on belongs here."""

from __future__ import annotations

import pytest

from leadgen.compliance import forget, suppression_hash
from leadgen.models import LEAD_COLUMNS, Lead, RunStats, SuppressionEntry
from leadgen.store.sqlite_store import SQLiteStore


@pytest.fixture(params=["sqlite", "sheets", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SQLiteStore(tmp_path / "leads.db")
        yield s
        s.close()
    elif request.param == "sheets":
        from test_sheets_store import make_store

        yield make_store()[0]
    else:
        yield request.getfixturevalue("pg_store")


def full_lead(lead_id: str = "ld_full", **kw) -> Lead:
    values = {c: f"{c}-value" for c in LEAD_COLUMNS}
    values.update(
        lead_id=lead_id, status="qualified", email_status="valid", email_type="role",
        domain=f"{lead_id}.example.org", email=f"info@{lead_id}.example.org",
        last_contacted_at="2026-11-10T13:53:43+00:00", next_follow_up_at="2026-11-17T09:00:00+00:00",
        created_at="2026-11-01T08:00:00+00:00", updated_at="2026-11-02T08:00:00+00:00",
    )  # fmt: skip
    values.update(kw)
    return Lead(**values)


def by_id(store, lead_id: str) -> Lead:
    return next(lead for lead in store.load_leads() if lead.lead_id == lead_id)


def test_round_trip_keeps_every_field(store):
    lead = full_lead()
    assert store.add_leads([lead]) == 1
    assert by_id(store, "ld_full").to_row() == lead.to_row()


def test_add_skips_existing_lead_ids_and_counts_inserts(store):
    a, b = full_lead("ld_a"), full_lead("ld_b")
    assert store.add_leads([a, b]) == 2
    assert store.add_leads([full_lead("ld_a", company_name="changed"), full_lead("ld_c")]) == 1
    assert sorted(lead.lead_id for lead in store.load_leads()) == ["ld_a", "ld_b", "ld_c"]
    assert by_id(store, "ld_a").company_name == "company_name-value"  # existing row untouched
    assert store.add_leads([]) == 0


def test_update_whitelists_fields_and_stamps_updated_at(store):
    store.add_leads([full_lead("ld_a")])
    n = store.update_leads({"ld_a": {"status": "contacted", "notes": "sent intro", "lead_id": "hijack", "bogus": "x"},
                            "ld_missing": {"status": "won"}})  # fmt: skip
    assert n == 1
    lead = by_id(store, "ld_a")
    assert (lead.status, lead.notes, lead.lead_id) == ("contacted", "sent intro", "ld_a")
    assert lead.updated_at and lead.updated_at != "2026-11-02T08:00:00+00:00"


def test_delete(store):
    store.add_leads([full_lead("ld_a"), full_lead("ld_b")])
    assert store.delete_leads(["ld_a", "ld_missing"]) == 1
    assert [lead.lead_id for lead in store.load_leads()] == ["ld_b"]


def test_suppression_is_idempotent_and_listable(store):
    entries = [SuppressionEntry(suppression_hash("email", "x@acme.com"), "email", "erasure request",
                                "2026-11-01T00:00:00+00:00"),
               SuppressionEntry(suppression_hash("domain", "acme.com"), "domain", "purged:do_not_contact",
                                "2026-11-02T00:00:00+00:00")]  # fmt: skip
    store.add_suppression(entries)
    store.add_suppression(entries[:1])
    assert store.load_suppression() == {e.hash for e in entries}
    listed = {e.hash: e for e in store.list_suppression()}
    assert len(listed) == 2
    assert (listed[entries[1].hash].kind, listed[entries[1].hash].reason) == (
        "domain",
        "purged:do_not_contact",
    )


def test_activities(store):
    store.add_leads([full_lead("ld_a"), full_lead("ld_b")])
    store.add_activity("ld_a", "email_sent", "step 1", "q1")
    store.add_activity("ld_b", "call")
    store.add_activity("ld_a", "reply", "interested")
    acts = store.list_activities("ld_a")
    assert [a["type"] for a in acts] == ["email_sent", "reply"]
    assert acts[0]["campaign"] == "q1" and acts[0]["timestamp"]
    assert len(store.list_activities(None)) == 3
    assert store.delete_activities(["ld_a"]) == 2
    assert [a["type"] for a in store.list_activities(None)] == ["call"]


def test_runs_newest_first_with_limit(store):
    for i, day in enumerate(("01", "02", "03")):
        store.log_run(RunStats(run_id=f"run_{i}", started_at=f"2026-11-{day}T06:00:00+00:00",
                               finished_at=f"2026-11-{day}T06:30:00+00:00", search="dental-uk",
                               sources="osm", new=i, notes="n"))  # fmt: skip
    runs = store.list_runs(2)
    assert [r["run_id"] for r in runs] == ["run_2", "run_1"]
    assert (runs[0]["new"], runs[0]["search"], runs[0]["started_at"]) == (
        "2",
        "dental-uk",
        "2026-11-03T06:00:00+00:00",
    )


def test_forget_domain_deletes_lead_activities_and_suppresses(store):
    store.add_leads([full_lead("ld_a", domain="acme.com", email="info@acme.com"), full_lead("ld_b")])
    store.add_activity("ld_a", "email_sent")
    result = forget(store, "acme.com")
    assert result.leads_deleted == 1
    assert [lead.lead_id for lead in store.load_leads()] == ["ld_b"]
    assert store.list_activities("ld_a") == []
    assert suppression_hash("domain", "acme.com") in store.load_suppression()
