"""Google Places API (New) Text Search — the official, permitted way to use Google Maps data.

Terms note: Google Maps Platform terms restrict caching/storing Places content (place IDs are
exempt). By default (`google_places.store_place_details: false`) Places is used for DISCOVERY:
the place ID and Maps link are stored; name/phone/address are taken from the business's own
website. Set it to true only after reviewing the terms for your use case.
Billing: requires a Google Cloud project with billing enabled; a free monthly usage cap applies
per SKU. Website/phone fields are in a higher-priced SKU with a smaller free cap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from leadgen.config import GooglePlacesConfig
from leadgen.http.fetcher import PoliteFetcher
from leadgen.logging_setup import get_logger
from leadgen.models import Candidate, SearchQuery
from leadgen.sources.base import SourceError

log = get_logger("google_places")

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.websiteUri", "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.types",
    "places.primaryType", "places.googleMapsUri", "places.businessStatus", "nextPageToken",
])  # fmt: skip
MAX_RESULTS_PER_QUERY = 60  # API limit: 3 pages x 20


class GooglePlacesSource:
    name = "google_places"

    def __init__(self, fetcher: PoliteFetcher, api_key: str | None, cfg: GooglePlacesConfig) -> None:
        self.fetcher = fetcher
        self.api_key = api_key
        self.cfg = cfg
        self.fetcher.limiter.set_delay("places.googleapis.com", 0.5)

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "GOOGLE_PLACES_API_KEY is not set"
        return True, ""

    async def _search(self, body: dict[str, Any], field_mask: str = FIELD_MASK) -> dict[str, Any]:
        try:
            resp = await self.fetcher.api_request(
                "POST",
                SEARCH_URL,
                json=body,
                headers={"X-Goog-Api-Key": self.api_key or "", "X-Goog-FieldMask": field_mask},
            )
        except httpx.HTTPStatusError as e:
            try:
                msg = e.response.json().get("error", {}).get("message", "")
            except ValueError:
                msg = e.response.text[:200]
            raise SourceError(f"Google Places API {e.response.status_code}: {msg}") from e
        return resp.json()

    async def ping(self) -> None:
        """Cheapest possible call (IDs only) to verify the key works."""
        await self._search({"textQuery": "coffee in London", "pageSize": 1}, field_mask="places.id")

    async def discover(self, query: SearchQuery) -> AsyncIterator[Candidate]:
        wanted = min(query.max_results, MAX_RESULTS_PER_QUERY)
        token: str | None = None
        yielded = 0
        while yielded < wanted:
            body: dict[str, Any] = {
                "textQuery": f"{query.keyword} in {query.location}",
                "pageSize": min(20, wanted - yielded),
                "languageCode": self.cfg.language_code,
            }
            if token:
                body["pageToken"] = token
            data = await self._search(body)
            for place in data.get("places", []) or []:
                if place.get("businessStatus") == "CLOSED_PERMANENTLY":
                    continue
                yield self._to_candidate(place)
                yielded += 1
                if yielded >= wanted:
                    break
            token = data.get("nextPageToken")
            if not token:
                break

    def _to_candidate(self, p: dict[str, Any]) -> Candidate:
        comps: dict[str, dict[str, Any]] = {}
        for c in p.get("addressComponents", []) or []:
            for t in c.get("types", []):
                comps.setdefault(t, c)
        city = (comps.get("locality") or comps.get("postal_town") or {}).get("longText", "")
        types = list(p.get("types", []) or [])
        if p.get("primaryType") and p["primaryType"] not in types:
            types.insert(0, p["primaryType"])
        elif p.get("primaryType"):
            types.remove(p["primaryType"])
            types.insert(0, p["primaryType"])
        return Candidate(
            source=self.name,
            company_name=(p.get("displayName") or {}).get("text"),
            website=p.get("websiteUri"),
            phone=p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber"),
            address=p.get("formattedAddress"),
            city=city,
            region=(comps.get("administrative_area_level_1") or {}).get("longText", ""),
            postcode=(comps.get("postal_code") or {}).get("longText", ""),
            country=(comps.get("country") or {}).get("shortText", ""),
            categories=types,
            external_id=f"places:{p.get('id', '')}",
            source_url=p.get("googleMapsUri"),
            restricted=not self.cfg.store_place_details,
        )
