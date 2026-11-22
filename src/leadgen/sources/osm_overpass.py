"""OpenStreetMap discovery: geocode the location with Nominatim, then query Overpass for businesses.

Data is © OpenStreetMap contributors, licensed under ODbL — storing it is allowed with attribution
(keep the source_url / external_id columns). Usage policies honoured: identifying User-Agent,
max 1 request/second to Nominatim, cached geocodes, modest Overpass query rate.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from leadgen.config import OsmConfig
from leadgen.extract.industry import IndustryMap
from leadgen.http.fetcher import PoliteFetcher, RetryableStatusError
from leadgen.logging_setup import get_logger
from leadgen.models import Candidate, SearchQuery
from leadgen.sources.base import SourceError
from leadgen.store.state import CrawlState

log = get_logger("osm")

CATEGORY_KEYS = ("amenity", "shop", "office", "craft", "healthcare", "tourism", "leisure")
TAG_RE = re.compile(r"^[a-z0-9_:]+(=[^\"\\]+)?$", re.I)


def api_error(service: str, resp: httpx.Response) -> str:
    """Short, actionable message instead of a raw HTML error page."""
    body = " ".join(re.sub(r"<[^>]+>", " ", resp.text).split())[:160]
    msg = f"{service} HTTP {resp.status_code}: {body}"
    if resp.status_code in (403, 406):
        msg += (
            f" — the {service} server refused this client. Check its usage policy, make sure `user_agent` "
            "names your application with a contact URL/email, or point the URL in the `osm:` config at "
            "another instance you are permitted to use (e.g. self-hosted)."
        )
    return msg


# Countries covered by Europe-only Overpass servers (Geofabrik's Europe extract). Russia and Turkey are
# left out on purpose: they are not fully in that extract, so they go to a `world` server instead.
EUROPE = frozenset(
    [
        "AD",
        "AL",
        "AT",
        "BA",
        "BE",
        "BG",
        "BY",
        "CH",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FO",
        "FR",
        "GB",
        "GG",
        "GI",
        "GR",
        "HR",
        "HU",
        "IE",
        "IM",
        "IS",
        "IT",
        "JE",
        "LI",
        "LT",
        "LU",
        "LV",
        "MC",
        "MD",
        "ME",
        "MK",
        "MT",
        "NL",
        "NO",
        "PL",
        "PT",
        "RO",
        "RS",
        "SE",
        "SI",
        "SK",
        "SM",
        "UA",
        "VA",
        "XK",
    ]
)


def _describe_failure(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timed out (server busy)"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect"
    if isinstance(exc, RetryableStatusError):
        return f"HTTP {exc.status} (busy)"
    return type(exc).__name__


def _first(value: str | None) -> str | None:
    """OSM allows several values separated by ';' — take the first."""
    if not value:
        return None
    return value.split(";")[0].strip() or None


class OsmOverpassSource:
    name = "osm"

    def __init__(
        self,
        fetcher: PoliteFetcher,
        cfg: OsmConfig,
        industries: IndustryMap,
        state: CrawlState | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.cfg = cfg
        self.industries = industries
        self.state = state
        self._unavailable: set[str] = set()  # Overpass servers that failed during this run
        nominatim_host = urlsplit(cfg.nominatim_url).hostname or ""
        self.fetcher.limiter.set_delay(
            nominatim_host, max(1.1, self.fetcher.limiter.delay_for(nominatim_host))
        )

    def available(self) -> tuple[bool, str]:
        return True, ""

    def servers_for(self, country_code: str) -> list[str]:
        """Configured Overpass servers able to answer for this country, in configured order."""
        cc = (country_code or "").upper()
        return [s.url for s in self.cfg.servers() if s.region == "world" or cc in EUROPE]

    async def _query_overpass(self, ql: str, geo: dict[str, Any], location: str) -> httpx.Response:
        """Try each suitable server once (all retries if there is only one). A server that times out,
        can't be reached or refuses us is skipped for the rest of this run, so one slow public server
        doesn't cost minutes per location."""
        servers = self.servers_for(geo.get("country_code", ""))
        if not servers:
            raise SourceError(
                f"no Overpass server in osm.overpass_servers covers '{location}' "
                f"(country {geo.get('country_code') or '?'}); add one with region: world"
            )
        attempts = 1 if len(servers) > 1 else 2  # with a fallback, move on quickly
        problems: list[str] = []
        for url in servers:
            host = urlsplit(url).hostname or url
            if url in self._unavailable:
                problems.append(f"{host} skipped (failed earlier in this run)")
                continue
            try:
                return await self.fetcher.api_request(
                    "POST", url, data={"data": ql}, timeout=self.cfg.timeout_s + 15, attempts=attempts,
                    deadline_s=self.cfg.timeout_s + 15,  # Overpass itself gives up after timeout_s
                )  # fmt: skip
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (403, 406):
                    self._unavailable.add(url)
                if len(servers) == 1:
                    raise SourceError(api_error("Overpass", e.response)) from e
                problems.append(f"{host}: HTTP {e.response.status_code}")
            except (httpx.TransportError, RetryableStatusError) as e:
                self._unavailable.add(url)
                problems.append(f"{host}: {_describe_failure(e)}")
        raise SourceError(
            "no Overpass server answered (" + "; ".join(problems) + "). The public servers are busy: "
            "retry later, or add another server under osm.overpass_servers in config.yaml"
        )

    async def geocode(self, location: str) -> dict[str, Any] | None:
        if self.state and (cached := self.state.get_geocode(location)):
            return cached
        try:
            resp = await self.fetcher.api_request(
                "GET",
                self.cfg.nominatim_url,
                params={"q": location, "format": "jsonv2", "limit": 1, "addressdetails": 1},
                headers={"Accept-Language": "en"},
            )
        except httpx.HTTPStatusError as e:
            raise SourceError(api_error("Nominatim", e.response)) from e
        results = resp.json()
        if not results:
            return None
        r = results[0]
        addr = r.get("address", {}) or {}
        geo = {
            "osm_type": r.get("osm_type", ""),
            "osm_id": int(r.get("osm_id", 0)),
            "lat": float(r.get("lat", 0)),
            "lon": float(r.get("lon", 0)),
            "display_name": r.get("display_name", ""),
            "city": addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("municipality")
            or "",
            "region": addr.get("state") or addr.get("county") or "",
            "country_code": (addr.get("country_code") or "").upper(),
        }
        if self.state:
            self.state.put_geocode(location, geo)
        return geo

    def build_query(self, geo: dict[str, Any], tags: list[str], keyword: str, limit: int) -> str:
        area = ""
        if geo["osm_type"] == "relation":
            area, scope = f"area(id:{3_600_000_000 + geo['osm_id']})->.a;", "(area.a)"
        elif geo["osm_type"] == "way":
            area, scope = f"area(id:{2_400_000_000 + geo['osm_id']})->.a;", "(area.a)"
        else:
            scope = f"(around:{self.cfg.point_radius_m},{geo['lat']},{geo['lon']})"

        selectors: list[str] = []
        for tag in tags:
            if not TAG_RE.match(tag):
                log.warning("ignoring malformed OSM tag", extra={"tag": tag})
                continue
            if "=" in tag:
                k, v = tag.split("=", 1)
                selectors.append(f'nwr["{k}"="{v}"]{scope};')
            else:
                selectors.append(f'nwr["{tag}"]{scope};')
        if not selectors:
            safe = re.sub(r"[^\w\s-]", "", keyword).strip()
            if not safe:
                raise SourceError(
                    f"no OSM tags known for keyword '{keyword}'; set osm_tags in the search config"
                )
            selectors.append(f'nwr["name"~"{safe}",i]{scope};')
        return f"[out:json][timeout:{self.cfg.timeout_s}];{area}({''.join(selectors)});out tags {limit};"

    async def discover(self, query: SearchQuery) -> AsyncIterator[Candidate]:
        geo = await self.geocode(query.location)
        if not geo:
            raise SourceError(f"could not geocode location '{query.location}'")

        tags = query.osm_tags or self.industries.osm_tags_for(query.keyword, query.industry)
        ql = self.build_query(geo, tags, query.keyword, query.max_results)
        log.debug("overpass query", extra={"query": ql})
        resp = await self._query_overpass(ql, geo, query.location)
        data = resp.json()
        if remark := data.get("remark"):
            log.warning("overpass remark (results may be partial)", extra={"remark": remark})

        for el in data.get("elements", []):
            if cand := self._to_candidate(el, geo):
                yield cand

    @staticmethod
    def _to_candidate(el: dict[str, Any], geo: dict[str, Any]) -> Candidate | None:
        tags: dict[str, str] = el.get("tags", {}) or {}
        name = tags.get("name") or tags.get("brand")
        if not name:
            return None
        website = _first(tags.get("website") or tags.get("contact:website") or tags.get("url"))
        email = _first(tags.get("email") or tags.get("contact:email"))
        phone = _first(tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile"))
        if not (website or email or phone):
            return None  # no public contact route — not a usable lead
        street = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
        city = tags.get("addr:city") or geo.get("city") or ""
        postcode = tags.get("addr:postcode") or ""
        address = ", ".join(filter(None, [street, postcode, tags.get("addr:city")]))
        osm_ref = f"{el.get('type')}/{el.get('id')}"
        return Candidate(
            source="osm",
            company_name=name,
            website=website,
            email=email,
            phone=phone,
            address=address,
            city=city,
            region=tags.get("addr:state") or geo.get("region") or "",
            postcode=postcode,
            country=tags.get("addr:country") or geo.get("country_code") or "",
            categories=[f"{k}={tags[k]}" for k in CATEGORY_KEYS if k in tags],
            external_id=f"osm:{osm_ref}",
            source_url=f"https://www.openstreetmap.org/{osm_ref}",
        )
