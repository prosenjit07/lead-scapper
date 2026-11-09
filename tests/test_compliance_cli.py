from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import yaml
from typer.testing import CliRunner

from leadgen.cli import app
from leadgen.compliance import purge, suppression_hash
from leadgen.models import Lead
from leadgen.store.sqlite_store import SQLiteStore

runner = CliRunner()


def _ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()


def test_purge_keeps_active_and_suppresses_opt_outs(tmp_path):
    store = SQLiteStore(tmp_path / "l.db")
    store.add_leads([
        Lead(lead_id="old_new", status="new", updated_at=_ago(400)),
        Lead(lead_id="old_won", status="won", updated_at=_ago(400)),
        Lead(lead_id="old_unsub", status="unsubscribed", email="x@acme.com", updated_at=_ago(400)),
        Lead(lead_id="old_dnc", status="do_not_contact", domain="spam.com", updated_at=_ago(400)),
        Lead(lead_id="recent", status="new", updated_at=_ago(10)),
    ])  # fmt: skip
    preview = purge(store, 365, dry_run=True)
    assert sorted(preview.lead_ids) == ["old_dnc", "old_new", "old_unsub"] and len(store.load_leads()) == 5

    result = purge(store, 365)
    assert result.deleted == 3
    assert sorted(lead.lead_id for lead in store.load_leads()) == ["old_won", "recent"]
    hashes = store.load_suppression()
    assert (
        suppression_hash("email", "x@acme.com") in hashes and suppression_hash("domain", "spam.com") in hashes
    )


def _setup_cli(tmp_path):
    cfg = {
        "user_agent": "TestLeadBot/1.0 (+https://test.invalid/bot)",
        "store": "sqlite",
        "sqlite_path": str(tmp_path / "leads.db"),
        "state_path": str(tmp_path / "state.db"),
        "log_dir": str(tmp_path / "logs"),
        "searches": [{"name": "s1", "keywords": ["dentist"], "locations": ["Bristol, UK"]}],
        "schedules": [{"search": "s1", "cron": "0 6 * * MON"}],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    store = SQLiteStore(tmp_path / "leads.db")
    store.add_leads([
        Lead(lead_id="ld_1", company_name="Acme Dental", email="info@acme.com", email_status="valid",
             domain="acme.com", industry="Healthcare / Dental", country="United Kingdom", city="Bristol"),
        Lead(lead_id="ld_2", company_name="Law & Co", email="bad@x.com", email_status="invalid",
             domain="lawco.com", industry="Legal Services", country="United Kingdom", city="Leeds"),
        Lead(lead_id="ld_3", company_name="Gone Ltd", email="hi@gone.com", email_status="valid",
             status="unsubscribed", industry="Legal Services", country="Germany", city="Berlin"),
    ])  # fmt: skip
    store.close()
    return ["--config", str(path), "--store", "sqlite"]


def test_cli_list_status_export_forget(tmp_path):
    base = _setup_cli(tmp_path)

    r = runner.invoke(app, [*base, "list", "--industry", "legal services", "--format", "json"])
    assert r.exit_code == 0, r.output
    assert sorted(x["lead_id"] for x in json.loads(r.output)) == ["ld_2", "ld_3"]

    r = runner.invoke(app, [*base, "list", "--group-by", "country"])
    assert r.exit_code == 0 and "United Kingdom" in r.output

    r = runner.invoke(app, [*base, "status", "info@acme.com", "contacted", "--note", "sent intro"])
    assert r.exit_code == 0, r.output
    store = SQLiteStore(tmp_path / "leads.db")
    lead = next(lead for lead in store.load_leads() if lead.lead_id == "ld_1")
    assert lead.status == "contacted" and lead.last_contacted_at and "sent intro" in lead.notes
    assert store.list_activities("ld_1")[0]["type"] == "status_change"

    r = runner.invoke(app, [*base, "status", "ld_1", "bogus"])
    assert r.exit_code != 0

    out = tmp_path / "export.csv"
    r = runner.invoke(app, [*base, "export", "--out", str(out)])
    assert r.exit_code == 0, r.output
    exported = out.read_text().splitlines()
    assert len(exported) == 2 and "ld_1" in exported[1]  # invalid + unsubscribed excluded

    r = runner.invoke(app, [*base, "forget", "acme.com", "--yes"])
    assert r.exit_code == 0, r.output
    assert "1 leads deleted" in r.output
    assert suppression_hash("domain", "acme.com") in store.load_suppression()


def test_cli_misc(tmp_path):
    base = _setup_cli(tmp_path)
    r = runner.invoke(app, [*base, "scheduler", "list"])
    assert r.exit_code == 0 and "s1" in r.output
    r = runner.invoke(app, [*base, "purge", "--dry-run"])
    assert r.exit_code == 0 and "0 leads" in r.output
    r = runner.invoke(app, [*base, "run"])
    assert r.exit_code != 0  # needs a search, --all, or --keyword/--location
    r = runner.invoke(app, [*base, "run", "nope"])
    assert r.exit_code == 2 and "no search named" in r.output


def test_cli_missing_config(tmp_path):
    r = runner.invoke(app, ["--config", str(tmp_path / "missing.yaml"), "list"])
    assert r.exit_code == 2 and "config file not found" in r.output


def test_cli_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "config" / "config.yaml").exists() and (tmp_path / ".env").exists()
    r = runner.invoke(app, ["list"])  # default sheets store without credentials -> clear error
    assert r.exit_code == 2 and "GOOGLE_SHEET_ID" in r.output
