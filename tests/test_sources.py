import json

import pytest
import respx

from leadgen.config import GooglePlacesConfig, OsmConfig
from leadgen.extract.industry import IndustryMap
from leadgen.http.fetcher import PoliteFetcher
from leadgen.models import SearchQuery
from leadgen.sources.base import SourceError
from leadgen.sources.google_places import SEARCH_URL, GooglePlacesSource
from leadgen.sources.osm_overpass import OsmOverpassSource
from leadgen.sources.seed_urls import SeedUrlSource, read_seed_file
from leadgen.store.state import CrawlState

UA = "TestLeadBot/1.0 (+https://test.invalid/bot)"


async def collect(source, query):
    return [c async for c in source.discover(query)]


@respx.mock
async def test_osm_geocodes_queries_and_maps_candidates(crawl_cfg, tmp_path):
    cfg = OsmConfig()
    respx.get(cfg.nominatim_url).respond(200, json=[{
        "osm_type": "relation", "osm_id": 57539, "lat": "51.45", "lon": "-2.58", "display_name": "Bristol, UK",
        "address": {"city": "Bristol", "state": "England", "country_code": "gb"}}])  # fmt: skip
    overpass = respx.post(cfg.overpass_url).respond(200, json={"elements": [
        {"type": "node", "id": 1, "tags": {"name": "Acme Dental", "amenity": "dentist", "website": "acmedental.co.uk",
                                           "email": "info@acmedental.co.uk;other@x.com", "addr:city": "Bristol"}},
        {"type": "way", "id": 2, "tags": {"name": "No Contact Dental", "amenity": "dentist"}},
        {"type": "node", "id": 3, "tags": {"amenity": "dentist", "phone": "+44 117"}},
        {"type": "node", "id": 4, "tags": {"name": "Phone Only Dental", "healthcare": "dentist", "phone": "+44 117 000"}},
    ]})  # fmt: skip
    state = CrawlState(tmp_path / "s.db")
    async with PoliteFetcher(crawl_cfg, UA, "TestLeadBot") as f:
        src = OsmOverpassSource(f, cfg, IndustryMap.load(), state)
        cands = await collect(src, SearchQuery(keyword="dentist", location="Bristol, UK", max_results=10))

    query = overpass.calls.last.request.content.decode()
    assert "area%28id%3A3600057539%29" in query or "area(id:3600057539)" in query
    assert "amenity" in query and "dentist" in query
    assert [c.company_name for c in cands] == ["Acme Dental", "Phone Only Dental"]
    acme = cands[0]
    assert acme.email == "info@acmedental.co.uk" and acme.website == "acmedental.co.uk"
    assert acme.country == "GB" and acme.categories == ["amenity=dentist"]
    assert acme.source_url == "https://www.openstreetmap.org/node/1" and not acme.restricted
    assert state.get_geocode("Bristol, UK")["osm_id"] == 57539  # cached for next time


def test_osm_query_for_point_and_unknown_keyword(crawl_cfg):
    src = OsmOverpassSource(PoliteFetcher(crawl_cfg, UA, "T"), OsmConfig(), IndustryMap.load())
    geo = {"osm_type": "node", "osm_id": 9, "lat": 1.0, "lon": 2.0}
    q = src.build_query(geo, [], 'zebra "grooming"', 5)
    assert "(around:10000,1.0,2.0)" in q and 'nwr["name"~"zebra grooming",i]' in q and "out tags 5;" in q
    assert 'nwr["office"="lawyer"]' in src.build_query(geo, ["office=lawyer", 'bad"tag'], "", 5)


@respx.mock
async def test_google_places_pagination_restriction_and_closed(crawl_cfg):
    page1 = {"places": [
        {"id": "p1", "displayName": {"text": "Acme Dental"}, "websiteUri": "https://acmedental.co.uk/",
         "types": ["dentist", "health"], "primaryType": "dentist", "googleMapsUri": "https://maps.google.com/?cid=1",
         "addressComponents": [{"longText": "Bristol", "shortText": "Bristol", "types": ["postal_town"]},
                               {"longText": "United Kingdom", "shortText": "GB", "types": ["country"]}]},
        {"id": "p2", "displayName": {"text": "Closed Dental"}, "businessStatus": "CLOSED_PERMANENTLY"},
    ], "nextPageToken": "tok"}  # fmt: skip
    page2 = {"places": [{"id": "p3", "displayName": {"text": "Smile Co"}, "types": ["dentist"]}]}
    route = respx.post(SEARCH_URL).mock(side_effect=[
        respx.MockResponse(200, json=page1), respx.MockResponse(200, json=page2)])  # fmt: skip
    async with PoliteFetcher(crawl_cfg, UA, "T") as f:
        src = GooglePlacesSource(f, "KEY", GooglePlacesConfig())
        cands = await collect(src, SearchQuery(keyword="dentist", location="Bristol, UK", max_results=10))

    assert [c.external_id for c in cands] == ["places:p1", "places:p3"]
    assert cands[0].city == "Bristol" and cands[0].country == "GB" and cands[0].categories[0] == "dentist"
    assert all(c.restricted for c in cands)  # default: discovery only
    second = json.loads(route.calls[1].request.content)
    assert second["pageToken"] == "tok" and second["textQuery"] == "dentist in Bristol, UK"
    assert route.calls[0].request.headers["X-Goog-Api-Key"] == "KEY"


@respx.mock
async def test_google_places_error_surfaces_message(crawl_cfg):
    respx.post(SEARCH_URL).respond(403, json={"error": {"message": "API key not valid"}})
    async with PoliteFetcher(crawl_cfg, UA, "T") as f:
        src = GooglePlacesSource(f, "BAD", GooglePlacesConfig())
        with pytest.raises(SourceError, match="API key not valid"):
            await collect(src, SearchQuery(keyword="x", location="y"))


def test_google_places_unavailable_without_key(crawl_cfg):
    src = GooglePlacesSource(PoliteFetcher(crawl_cfg, UA, "T"), None, GooglePlacesConfig())
    assert src.available() == (False, "GOOGLE_PLACES_API_KEY is not set")


async def test_seed_files(tmp_path):
    txt = tmp_path / "urls.txt"
    txt.write_text("# comment\nacme.com\n\nhttps://smile.co.uk/about\nnot a url\n")
    csv_file = tmp_path / "urls.csv"
    csv_file.write_text("Company_Name,Website,City\nAcme,acme.com,Bristol\nNo site,,Leeds\n")
    assert [r["website"] for r in read_seed_file(txt)] == [
        "acme.com",
        "https://smile.co.uk/about",
        "not a url",
    ]
    cands = await collect(SeedUrlSource(csv_file), SearchQuery(keyword="", location=""))
    assert len(cands) == 1 and cands[0].company_name == "Acme" and cands[0].website == "https://acme.com/"
    cands = await collect(SeedUrlSource(txt), SearchQuery(keyword="", location=""))
    assert [c.website for c in cands] == ["https://acme.com/", "https://smile.co.uk/about"]


@respx.mock
async def test_overpass_refusal_gives_short_actionable_error(crawl_cfg):
    cfg = OsmConfig()
    respx.get(cfg.nominatim_url).respond(
        200, json=[{"osm_type": "node", "osm_id": 1, "lat": "1", "lon": "2"}]
    )
    route = respx.post(cfg.overpass_url).respond(
        406, text="<html><head><title>406 Not Acceptable</title></head><body>nginx</body></html>"
    )
    async with PoliteFetcher(crawl_cfg, UA, "T") as f:
        src = OsmOverpassSource(f, cfg, IndustryMap.load())
        with pytest.raises(SourceError) as exc:
            await collect(src, SearchQuery(keyword="dentist", location="Somewhere"))
    msg = str(exc.value)
    assert msg.startswith("Overpass HTTP 406: 406 Not Acceptable nginx") and "<" not in msg
    assert "user_agent" in msg
    assert route.calls.last.request.headers["accept"] == "application/json"
