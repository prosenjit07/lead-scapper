"""REST API against a temporary SQLite store (no network: runs are faked)."""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

import pytest
import yaml

from leadgen.compliance import suppression_hash
from leadgen.config import AppConfig, Secrets
from leadgen.models import Lead, RunStats
from leadgen.pipeline import RunResult
from leadgen.store.sqlite_store import SQLiteStore

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from leadgen import api as api_module  # noqa: E402
from leadgen.api import create_app, openapi_text  # noqa: E402

DOCS = Path(__file__).parents[1] / "docs"


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({
        "user_agent": "TestLeadBot/1.0 (+https://test.invalid/bot)",
        "store": "sqlite",
        "sqlite_path": str(tmp_path / "leads.db"),
        "state_path": str(tmp_path / "state.db"),
        "log_dir": str(tmp_path / "logs"),
        "searches": [{"name": "s1", "keywords": ["dentist"], "locations": ["Bristol, UK"]},
                     {"name": "s2", "keywords": ["lawyer"], "locations": ["Leeds, UK"], "sources": ["osm"]}],
        "schedules": [{"search": "s1", "cron": "0 6 * * MON"}],
    })  # fmt: skip


def _secrets(api_key: str | None = None) -> Secrets:
    return Secrets(_env_file=None, google_service_account_file=None, google_sheet_id=None,
                   google_places_api_key=None, leadgen_api_key=api_key)  # fmt: skip


@pytest.fixture
def cfg(tmp_path):
    cfg = _cfg(tmp_path)
    store = SQLiteStore(cfg.sqlite_path)
    store.add_leads([
        Lead(lead_id="ld_1", company_name="Acme Dental", email="info@acme.com", email_status="valid",
             domain="acme.com", website="https://acme.com", industry="Healthcare / Dental",
             country="United Kingdom", city="Bristol", source="osm", created_at="2026-11-01T10:00:00+00:00"),
        Lead(lead_id="ld_2", company_name="Law & Co", email="bad@x.com", email_status="invalid",
             domain="lawco.com", industry="Legal Services", country="United Kingdom", city="Leeds",
             source="osm", created_at="2026-11-02T10:00:00+00:00"),
        Lead(lead_id="ld_3", company_name="Gone Ltd", email="hi@gone.com", email_status="valid",
             status="unsubscribed", domain="gone.com", industry="Legal Services", country="Germany",
             city="Berlin", source="google_places", created_at="2026-11-03T10:00:00+00:00"),
    ])  # fmt: skip
    store.close()
    return cfg


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg, _secrets())) as c:
        yield c


def test_list_filter_paginate(client):
    r = client.get("/leads")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3 and [x["lead_id"] for x in body["items"]] == ["ld_3", "ld_2", "ld_1"]

    r = client.get("/leads", params={"industry": "legal services", "limit": 1, "offset": 1})
    body = r.json()
    assert body["total"] == 2 and [x["lead_id"] for x in body["items"]] == ["ld_2"]

    r = client.get("/leads", params=[("city", "Bristol"), ("city", "Leeds"), ("email_status", "valid")])
    assert [x["lead_id"] for x in r.json()["items"]] == ["ld_1"]

    assert client.get("/leads", params={"q": "gone"}).json()["total"] == 1
    assert client.get("/leads", params={"status": "bogus"}).status_code == 422
    assert client.get("/leads", params={"limit": 0}).status_code == 422


def test_get_lead_by_id_email_or_domain(client):
    for ref in ("ld_1", "info@acme.com", "acme.com"):
        r = client.get(f"/leads/{ref}")
        assert r.status_code == 200 and r.json()["company_name"] == "Acme Dental"
    assert client.get("/leads/nope.com").status_code == 404


def test_patch_status_logs_activity(client, cfg):
    r = client.patch("/leads/acme.com", json={"status": "contacted", "append_note": "intro sent",
                                               "campaign": "q1", "sequence_step": "1"})  # fmt: skip
    assert r.status_code == 200, r.text
    lead = r.json()
    assert lead["status"] == "contacted" and lead["last_contacted_at"] and lead["campaign"] == "q1"
    assert "intro sent" in lead["notes"] and lead["updated_at"]

    acts = client.get("/leads/ld_1/activities").json()
    assert len(acts) == 1 and acts[0]["type"] == "status_change"
    assert acts[0]["details"] == "new -> contacted: intro sent"

    # same status again: no new activity; note-only append works; null clears a field
    r = client.patch(
        "/leads/ld_1", json={"status": "contacted", "append_note": "follow-up", "campaign": None}
    )
    assert "follow-up" in r.json()["notes"] and r.json()["campaign"] == ""
    assert len(client.get("/leads/ld_1/activities").json()) == 1

    # an explicit last_contacted_at wins over the automatic one
    r = client.patch("/leads/ld_2", json={"status": "contacted", "last_contacted_at": "2026-01-02"})
    assert r.json()["last_contacted_at"] == "2026-01-02"


@pytest.mark.parametrize("body", [
    {"status": "bogus"},
    {"email": "x@y.com"},  # crawler data is read-only
    {"next_follow_up_at": "next tuesday"},
    {"notes": "a", "append_note": "b"},
])  # fmt: skip
def test_patch_rejects_bad_input(client, body):
    assert client.patch("/leads/ld_1", json=body).status_code == 422


def test_activities(client):
    r = client.post("/leads/info@acme.com/activities",
                    json={"type": "email_sent", "details": "step 1", "campaign": "q1"})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["lead_id"] == "ld_1" and r.json()["type"] == "email_sent" and r.json()["timestamp"]
    assert client.get("/leads/ld_1/activities").json()[-1]["details"] == "step 1"
    assert client.post("/leads/ld_1/activities", json={"type": "Email Sent!"}).status_code == 422
    assert client.post("/leads/nope/activities", json={"type": "call"}).status_code == 404


def test_export_excludes_uncontactable(client):
    r = client.get("/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and r.headers["x-total-count"] == "1"
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["lead_id"] for row in rows] == ["ld_1"]  # invalid email + unsubscribed excluded

    r = client.get("/export", params={"format": "json", "include_opted_out": True})
    assert r.headers["content-type"] == "application/json" and len(json.loads(r.text)) == 3


def test_forget_domain(client, cfg):
    r = client.post("/privacy/forget", json={"identifier": "https://www.acme.com/contact"})
    assert r.status_code == 200, r.text
    assert r.json() == {"kind": "domain", "value": "acme.com", "leads_deleted": 1, "emails_scrubbed": 0}
    assert client.get("/leads/ld_1").status_code == 404
    store = SQLiteStore(cfg.sqlite_path)
    assert suppression_hash("domain", "acme.com") in store.load_suppression()
    assert client.post("/privacy/forget", json={"identifier": "not a domain"}).status_code == 422


def test_searches_and_run_history(client, cfg):
    searches = {s["name"]: s for s in client.get("/searches").json()}
    assert searches["s1"]["schedules"] == ["0 6 * * MON"] and searches["s1"]["next_run"]
    assert searches["s2"]["schedules"] == [] and searches["s2"]["next_run"] is None

    store = SQLiteStore(cfg.sqlite_path)
    store.log_run(RunStats(run_id="run_x", search="s1", sources="osm", new=4, errors=1, notes="boom"))
    runs = client.get("/runs").json()
    assert runs[0]["run_id"] == "run_x" and runs[0]["new"] == 4 and runs[0]["errors"] == 1


def _wait_for(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_start_run_in_background(client, monkeypatch):
    calls = []

    async def fake_run_search(cfg, secrets, store, search, **kwargs):
        calls.append((search.name, kwargs))
        return RunResult(stats=RunStats(run_id="run_fake", search=search.name, new=1),
                         leads=[Lead(lead_id="ld_new", company_name="New Co")])  # fmt: skip

    monkeypatch.setattr(api_module, "run_search", fake_run_search)
    r = client.post("/runs", json={"search": "s1", "dry_run": True, "limit": 5, "sources": ["osm"]})
    assert r.status_code == 202, r.text
    assert r.headers["location"] == f"/jobs/{r.json()['job_id']}"
    job = _wait_for(client, r.json()["job_id"])
    assert job["status"] == "succeeded" and job["stats"]["run_id"] == "run_fake"
    assert [x["lead_id"] for x in job["leads"]] == ["ld_new"]  # dry run returns the would-be leads
    assert calls == [("s1", {"sources": ["osm"], "limit": 5, "dry_run": True, "force_recrawl": False})]
    assert client.get("/jobs").json()[0]["leads"] == []  # list view stays small

    assert client.post("/runs", json={"search": "nope"}).status_code == 404
    assert client.post("/runs", json={"search": "s1", "sources": ["seed"]}).status_code == 422
    assert client.get("/jobs/job_missing").status_code == 404


def test_run_failure_and_overlap(client, monkeypatch):
    import threading

    release = threading.Event()

    async def slow_failing_run(*args, **kwargs):
        release.wait(5)
        raise RuntimeError("Overpass HTTP 406")

    monkeypatch.setattr(api_module, "run_search", slow_failing_run)
    job_id = client.post("/runs", json={"search": "s2"}).json()["job_id"]
    assert client.post("/runs", json={"search": "s2"}).status_code == 409  # never overlap a search
    release.set()
    job = _wait_for(client, job_id)
    assert job["status"] == "failed" and "Overpass HTTP 406" in job["error"]


def test_api_key(cfg):
    with TestClient(create_app(cfg, _secrets(api_key="s3cret"))) as c:
        assert c.get("/health").status_code == 200  # open
        assert c.get("/leads").status_code == 401
        assert c.get("/leads", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/leads", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.post("/privacy/forget", json={"identifier": "acme.com"}).status_code == 401


def test_health_and_docs(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "version": api_module.__version__,
        "store": "sqlite",
    }
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "leadgen API"


def test_store_errors_are_503(tmp_path):
    cfg = _cfg(tmp_path).model_copy(update={"store": "sheets"})
    with TestClient(create_app(cfg, _secrets())) as c:
        r = c.get("/leads")
        assert r.status_code == 503 and "GOOGLE_SHEET_ID" in r.json()["detail"]


@pytest.mark.parametrize("name", ["openapi.json", "swagger.yaml"])
def test_checked_in_spec_is_current(cfg, name):
    """docs/openapi.json and docs/swagger.yaml are generated from the code (see the assert message)."""
    current = create_app(cfg, _secrets()).openapi()
    text = (DOCS / name).read_text(encoding="utf-8")
    checked_in = yaml.safe_load(text) if name.endswith(".yaml") else json.loads(text)
    assert checked_in == current, (
        f"docs/{name} is stale; regenerate it with: uv run leads api --openapi docs/{name}"
    )


def test_openapi_text_yaml_is_readable(cfg):
    text = openapi_text(create_app(cfg, _secrets()), "yaml")
    assert text.startswith("openapi: 3.1.0\n") and "&id" not in text  # no YAML anchors
    assert "description: |" in text  # multi-line descriptions as readable blocks
