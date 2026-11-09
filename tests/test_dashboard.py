"""Render the Streamlit dashboard headlessly against a temporary SQLite store."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from leadgen.models import Lead
from leadgen.store.sqlite_store import SQLiteStore

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = Path(__file__).parents[1] / "src" / "leadgen" / "dashboard" / "app.py"


@pytest.fixture
def dashboard_env(tmp_path, monkeypatch):
    cfg = {
        "user_agent": "TestLeadBot/1.0 (+https://test.invalid/bot)",
        "store": "sqlite",
        "sqlite_path": str(tmp_path / "leads.db"),
        "state_path": str(tmp_path / "state.db"),
        "log_dir": str(tmp_path / "logs"),
        "searches": [{"name": "s1", "keywords": ["dentist"], "locations": ["Bristol, UK"]}],
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    store = SQLiteStore(tmp_path / "leads.db")
    store.add_leads([
        Lead(lead_id="ld_1", company_name="Acme Dental", email="info@acme.com", email_status="valid",
             industry="Healthcare / Dental", city="Bristol", country="United Kingdom", source="osm",
             created_at="2026-11-01T00:00:00+00:00"),
        Lead(lead_id="ld_2", company_name="Law & Co", email="", email_status="missing", status="contacted",
             industry="Legal Services", city="Leeds", country="United Kingdom", source="google_places",
             created_at="2026-11-02T00:00:00+00:00"),
        Lead(lead_id="ld_3", company_name="Gone Ltd", email="hi@gone.com", email_status="valid",
             status="unsubscribed", industry="Legal Services", city="Berlin", country="Germany", source="osm",
             created_at="2026-11-03T00:00:00+00:00"),
    ])  # fmt: skip
    store.close()
    monkeypatch.setenv("LEADGEN_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("LEADGEN_STORE", "sqlite")
    return tmp_path


def test_dashboard_renders_kpis_charts_and_table(dashboard_env):
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Leads"] == "3"
    assert metrics["Contactable now"] == "1"  # ld_2 has no email, ld_3 unsubscribed
    assert metrics["Valid email (of leads with email)"] == "100%"
    assert len(at.get("vega_lite_chart")) == 4  # industry, location, status, source
    assert len(at.get("dataframe")) >= 1  # table views + editable leads table


def test_dashboard_filter_narrows_kpis(dashboard_env):
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    industry = next(ms for ms in at.multiselect if ms.label == "Industry")
    industry.select("Legal Services").run()
    assert not at.exception, at.exception
    assert {m.label: m.value for m in at.metric}["Leads"] == "2"
