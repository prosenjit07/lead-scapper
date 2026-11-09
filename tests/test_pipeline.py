from __future__ import annotations

import httpx
import pytest
import respx

from conftest import fixture_text
from leadgen.compliance import forget, suppression_hash
from leadgen.extract.industry import IndustryMap
from leadgen.http.fetcher import PoliteFetcher
from leadgen.models import Candidate, Lead, SearchQuery, SuppressionEntry
from leadgen.pipeline import Pipeline
from leadgen.store.sqlite_store import SQLiteStore
from leadgen.store.state import CrawlState

HTML = {"content-type": "text/html; charset=utf-8"}
QUERY = SearchQuery(keyword="dentist", location="Bristol, UK", search_name="test", tags=["t1"])


class FakeSource:
    name = "osm"

    def __init__(self, cands: list[Candidate]) -> None:
        self.cands = cands

    def available(self):
        return True, ""

    async def discover(self, query):
        for c in self.cands:
            yield c


def acme(**kw) -> Candidate:
    base = dict(source="osm", company_name="Acme Dental", website="acmedental.co.uk", city="Bristol", country="GB",
                categories=["amenity=dentist"], external_id="osm:node/1",
                source_url="https://www.openstreetmap.org/node/1")  # fmt: skip
    return Candidate(**{**base, **kw})


def phone_only() -> Candidate:
    return Candidate(source="osm", company_name="Phone Only Dental", phone="0117 496 0999", city="Bristol",
                     country="GB", categories=["amenity=dentist"], external_id="osm:node/2")  # fmt: skip


def broken() -> Candidate:
    return Candidate(source="osm", company_name="Broken", website="https://brokensite.com", city="Bristol")


@pytest.fixture
def sites():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://acmedental.co.uk/robots.txt").respond(404)
        mock.get("https://acmedental.co.uk/").respond(
            301, headers={"location": "https://www.acmedental.co.uk/"}
        )
        mock.get("https://www.acmedental.co.uk/robots.txt").respond(
            200, text="User-agent: *\nDisallow: /team\n"
        )
        mock.get("https://www.acmedental.co.uk/").respond(
            200, text=fixture_text("acme_home.html"), headers=HTML
        )
        mock.get("https://www.acmedental.co.uk/contact").respond(
            200, text=fixture_text("acme_contact.html"), headers=HTML
        )
        mock.get("https://www.acmedental.co.uk/about-us/").respond(200, text="<p>About us</p>", headers=HTML)
        mock.get("https://brokensite.com/robots.txt").mock(side_effect=RuntimeError("boom"))
        yield mock


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "leads.db")
    yield s
    s.close()


async def run(app_cfg, store, validator, cands, *, dry_run=False, force=False, query=QUERY):
    state = CrawlState(app_cfg.state_path)
    try:
        async with PoliteFetcher(app_cfg.crawl, app_cfg.user_agent, app_cfg.user_agent_token) as f:
            p = Pipeline(cfg=app_cfg, store=store, state=state, fetcher=f, validator=validator,
                         industries=IndustryMap.load(), dry_run=dry_run)  # fmt: skip
            return await p.run([query], [FakeSource(cands)], "test", force_recrawl=force)
    finally:
        state.close()


def by_name(store, name) -> Lead:
    return next(lead for lead in store.load_leads() if lead.company_name == name)


async def test_end_to_end_extracts_validates_and_isolates_errors(app_cfg, store, validator, sites):
    result = await run(app_cfg, store, validator, [acme(), phone_only(), broken()])
    s = result.stats
    assert (s.discovered, s.new, s.errors, s.crawled) == (3, 2, 1, 1)
    assert "brokensite.com" in s.notes

    lead = by_name(store, "Acme Dental")
    assert lead.website == "https://www.acmedental.co.uk" and lead.domain == "acmedental.co.uk"
    assert (lead.email, lead.email_status, lead.email_type) == (
        "jane.smith@acmedental.co.uk",
        "valid",
        "personal",
    )
    assert lead.additional_emails.split(",") == ["info@acmedental.co.uk", "careers@acmedental.co.uk"]
    assert (lead.contact_name, lead.contact_title) == ("Jane Smith", "Owner & Principal Dentist")
    assert lead.phone == "+441174960000"  # from the site's JSON-LD
    assert (lead.city, lead.country, lead.industry) == ("Bristol", "United Kingdom", "Healthcare / Dental")
    assert lead.linkedin_url == "https://www.linkedin.com/company/acme-dental-bristol"
    assert lead.facebook_url == "https://www.facebook.com/AcmeDental"
    assert lead.twitter_url == "https://x.com/AcmeDentalUK"
    assert lead.instagram_url == "https://www.instagram.com/acmedental"
    assert lead.source_url == "https://www.acmedental.co.uk/contact"  # page where the email was found
    assert (lead.external_id, lead.lawful_basis, lead.tags, lead.status) == (
        "osm:node/1",
        "legitimate_interest",
        "t1",
        "new",
    )
    assert lead.run_id == s.run_id and lead.lead_id.startswith("ld_")

    other = by_name(store, "Phone Only Dental")
    assert (other.phone, other.email_status, other.website) == ("+441174960999", "missing", "")

    # robots.txt disallowed /team -> never requested
    assert not any(c.request.url.path == "/team" for c in sites.calls)
    assert store.list_runs()[0]["new"] == "2"


async def test_second_run_finds_duplicates(app_cfg, store, validator, sites):
    await run(app_cfg, store, validator, [acme(), phone_only()])
    calls_before = len(sites.calls)
    s = (await run(app_cfg, store, validator, [acme(), phone_only(),
                                               acme(source="google_places", company_name="ACME DENTAL LTD")])).stats  # fmt: skip
    assert (s.new, s.duplicates) == (0, 3)
    assert len(sites.calls) == calls_before  # known businesses are not re-crawled
    assert len(store.load_leads()) == 2


async def test_duplicate_merges_blank_fields_only(app_cfg, store, validator, sites):
    store.add_leads([Lead(lead_id="ld_existing", company_name="Acme", domain="acmedental.co.uk", status="contacted",
                          notes="spoke to Jane")])  # fmt: skip
    s = (await run(app_cfg, store, validator, [acme(phone="0117 496 0001")])).stats
    assert (s.new, s.duplicates, s.merged) == (0, 1, 1)
    lead = store.load_leads()[0]
    assert (lead.phone, lead.city, lead.company_name) == ("+441174960001", "Bristol", "Acme")  # name kept
    assert (lead.status, lead.notes) == ("contacted", "spoke to Jane")  # user fields untouched


async def test_forget_email_scrubs_and_is_never_recollected(app_cfg, store, validator, sites):
    await run(app_cfg, store, validator, [acme()])
    result = forget(store, "Jane.Smith@acmedental.co.uk")
    assert (result.leads_deleted, result.emails_scrubbed) == (0, 1)
    lead = store.load_leads()[0]
    assert lead.email == "info@acmedental.co.uk" and lead.contact_name == ""
    assert suppression_hash("email", "jane.smith@acmedental.co.uk") in store.load_suppression()

    # Wipe leads (but not suppression) and collect again: Jane's address must not come back.
    store.delete_leads([lead.lead_id])
    await run(app_cfg, store, validator, [acme()], force=True)
    again = store.load_leads()[0]
    assert again.email == "info@acmedental.co.uk"
    assert "jane.smith@acmedental.co.uk" not in again.additional_emails


async def test_forget_domain_deletes_and_suppresses(app_cfg, store, validator, sites):
    await run(app_cfg, store, validator, [acme(), phone_only()])
    assert forget(store, "https://acmedental.co.uk").leads_deleted == 1
    s = (await run(app_cfg, store, validator, [acme()], force=True)).stats
    assert (s.suppressed, s.new) == (1, 0)
    assert [lead.company_name for lead in store.load_leads()] == ["Phone Only Dental"]


async def test_role_only_policy(app_cfg, store, validator, sites):
    app_cfg.compliance.personal_data = "role_only"
    await run(app_cfg, store, validator, [acme()])
    lead = store.load_leads()[0]
    assert (lead.email, lead.email_type, lead.contact_name) == ("info@acmedental.co.uk", "role", "")
    assert "careers@acmedental.co.uk" in lead.additional_emails


async def test_dry_run_writes_nothing(app_cfg, store, validator, sites):
    result = await run(app_cfg, store, validator, [acme(), phone_only()], dry_run=True)
    assert len(result.leads) == 2 and store.load_leads() == [] and store.list_runs() == []


async def test_restricted_source_stores_only_website_facts(app_cfg, store, validator, sites):
    places = acme(source="google_places", company_name="Maps Name", city="Mapsville", phone="+44 1",
                  restricted=True, external_id="places:abc", source_url="https://maps.google.com/?cid=1")  # fmt: skip
    no_site = Candidate(source="google_places", company_name="No Site", city="Bristol", restricted=True)
    s = (await run(app_cfg, store, validator, [places, no_site])).stats
    assert (s.new, s.skipped) == (1, 1)
    lead = store.load_leads()[0]
    assert lead.company_name == "Acme Dental Ltd"  # from the business's own JSON-LD, not Places
    assert lead.city == "Bristol" and lead.phone == "+441174960000"
    assert lead.external_id == "places:abc"


async def test_require_email_and_suppressed_domain(app_cfg, store, validator, sites):
    app_cfg.compliance.require_email = True
    store.add_suppression([SuppressionEntry(hash=suppression_hash("domain", "acmedental.co.uk"), kind="domain",
                                            reason="test")])  # fmt: skip
    s = (await run(app_cfg, store, validator, [acme(), phone_only()])).stats
    assert (s.new, s.suppressed, s.skipped) == (0, 1, 1)


async def test_source_failure_is_recorded_not_raised(app_cfg, store, validator):
    class Exploding(FakeSource):
        async def discover(self, query):
            raise httpx.ConnectError("overpass down")
            yield  # pragma: no cover

    state = CrawlState(app_cfg.state_path)
    async with PoliteFetcher(app_cfg.crawl, app_cfg.user_agent, app_cfg.user_agent_token) as f:
        p = Pipeline(cfg=app_cfg, store=store, state=state, fetcher=f, validator=validator,
                     industries=IndustryMap.load())  # fmt: skip
        result = await p.run([QUERY], [Exploding([])], "test")
    state.close()
    assert result.stats.errors == 1 and "overpass down" in result.stats.notes
