# leadgen: compliant B2B lead discovery to Google Sheets

`leadgen` finds businesses that match your keywords and locations, then reads the **public contact pages on each business's own website**. It extracts the company name, email, contact person, phone, address, industry and social profiles, validates the emails, removes duplicates, and stores one row per company in **Google Sheets** (or a local SQLite file). The sheet is structured so a cold-email or CRM workflow can plug in later.

```
 searches (config.yaml)             discovery sources                   enrichment                     storage
┌─────────────────────┐   ┌──────────────────────────────┐   ┌──────────────────────────────┐   ┌──────────────────────┐
│ keywords × locations├──▶│ OpenStreetMap (Overpass)     │──▶│ polite website crawl         │──▶│ Google Sheets        │
│ schedules (cron)    │   │ Google Places API (New)      │   │  robots.txt · rate limits    │   │  Leads · Activities  │
└─────────────────────┘   │ your URL list (txt / csv)    │   │ emails · JSON-LD · socials   │   │  Suppression · Runs  │
                          └──────────────┬───────────────┘   │ email validation (DNS/MX)    │   │  Summary (formulas)  │
                                         │ early dedupe      │ industry taxonomy            │   │ — or SQLite —        │
                                         ▼                   └──────────────────────────────┘   └──────────▲───────────┘
                          suppression list (hashed opt-outs) ── final dedupe (email / domain / name+city) ──┘
```

Interfaces: **CLI** (`leads …`), **REST API** with Swagger docs (`leads api`), **Streamlit dashboard** (`leads dashboard`), **scheduler** (`leads scheduler start`, or cron).

---

## Compliance by design

This is engineering support for compliance, **not legal advice**. You are still the data controller.

| Topic | What the system does |
|---|---|
| **Google Maps** | Uses only the official **Places API (New)**, never Maps HTML scraping. Google's terms restrict storing Places content (place IDs are exempt), so by default (`google_places.store_place_details: false`) Places only *discovers* businesses. Stored details come from the business's own website, and the `place_id` is kept as the reference. |
| **LinkedIn** | **Not scraped.** LinkedIn's terms forbid it. LinkedIn and other social URLs are recorded only when the company links them on its own site (or in schema.org `sameAs`). |
| **robots.txt** | Checked for every page *and every redirect hop* (RFC 9309 rules: a missing robots.txt allows crawling, an unreachable one blocks it). `Crawl-delay` / `Request-rate` are honoured. |
| **Politeness** | One request at a time per site, ≥2 s apart; a few pages per site (contact/about/impressum/team); HTML only; 2 MB cap; no login pages; an identifying User-Agent you configure. Domains aren't re-crawled for 30 days. |
| **Hidden emails** | Cloudflare-protected or `name [at] domain [dot] com` addresses are **not decoded**. Hiding an address signals the owner doesn't want it harvested. |
| **Data minimisation** | Business contact data only, one row per company. `compliance.personal_data: role_only` keeps only shared mailboxes (info@, sales@), with no names and no personal LinkedIn, for stricter jurisdictions (e.g. DE/AT B2B email rules). |
| **Provenance (GDPR Art. 14/15)** | Every lead stores `source`, `source_url` (the page where the email was found), `external_id`, `created_at` and `lawful_basis`, so you can tell people where their data came from. |
| **Erasure (Art. 17)** | `leads forget jane@acme.com` (one person) or `leads forget acme.com` (whole company) deletes the data and adds a **SHA-256 hash** to the Suppression tab, so it is never collected or contacted again (without keeping the address itself). |
| **Retention** | `leads purge` deletes leads with no engagement older than `retention_days`. Opt-outs are turned into suppression hashes before deletion. |
| **Spreadsheet safety** | All writes use `RAW` input, so scraped text like `=IMPORTXML(...)` is stored as text and never executed as a formula. |

---

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (Python 3.11+; tested on 3.13).

```bash
cd ~/lead-scraper
uv sync --extra dashboard          # add --extra browser for optional JS rendering
uv run leads init                  # creates config/config.yaml, .env, data/, logs/, secrets/, exports/
# edit config/config.yaml: set user_agent to YOUR bot name + contact URL/email; define searches
# edit .env: Google credentials (see below)
uv run leads doctor                # checks config, credentials, APIs, DNS
uv run leads sheet init            # creates tabs, headers, status dropdowns, Summary formulas
uv run leads run dental-uk --dry-run --limit 10    # preview without saving
uv run leads run dental-uk                          # for real
```

No Google account yet? Add `--store sqlite` to any command (or set `store: sqlite`) to use a local database.

### Google Sheets setup (free)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, then enable the **Google Sheets API** and the **Google Drive API**.
2. Go to *IAM & Admin → Service accounts → Create*, then *Keys → Add key → JSON*. Save the file as `secrets/service-account.json`.
3. Create an empty Google Sheet. **Share it with the service account's email** (shown by `leads doctor`) as **Editor**.
4. Put the sheet ID (the long part of its URL between `/d/` and `/edit`) in `.env` as `GOOGLE_SHEET_ID`.

### Google Places API setup (optional)

1. In the same project, enable **Places API (New)**. This **requires a billing account**. Google applies a free monthly usage cap per SKU; the website and phone fields fall in a higher-priced SKU with a smaller free cap. Check current pricing and set a budget alert.
2. Create an API key, restrict it to *Places API (New)*, and put it in `.env` as `GOOGLE_PLACES_API_KEY`.
3. Without a key, the `google_places` source is skipped with a warning and OSM still runs.

---

## Configuration (`config/config.yaml`)

```yaml
user_agent: "AcmeLeadBot/1.0 (+https://acme.com/bot; contact@acme.com)"
store: sheets
merge_duplicates: true            # fill BLANK fields of existing rows; never touches status/notes/owner/etc.
crawl: {max_pages_per_site: 6, per_domain_delay_s: 2, concurrency: 10, recrawl_after_days: 30}
compliance: {personal_data: allow, retention_days: 365, lawful_basis: legitimate_interest, require_email: false}
searches:
  - name: dental-uk
    keywords: ["dentist"]
    locations: ["Bristol, UK", "Leeds, UK"]
    sources: [osm, google_places]
    industry: "Healthcare / Dental"   # optional label override
    osm_tags: ["amenity=dentist"]     # optional; otherwise derived from the industry taxonomy
    max_results: 100                  # per keyword × location × source (Places caps at 60)
    tags: [q1-campaign]
schedules:
  - {search: dental-uk, cron: "0 6 * * MON"}
```

- **Industry taxonomy**: `src/leadgen/data/industries.yaml` maps keywords, OSM tags, Google place types and schema.org types to about 30 normalized industries. Extend it with `industries_file:` (same format).
- **OSM tags**: if a keyword has no mapping, the OSM source falls back to a business-name search. Set `osm_tags` for precise results (see the [OSM Map Features](https://wiki.openstreetmap.org/wiki/Map_features) wiki).
- Secrets live only in `.env`: `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEET_ID`, `GOOGLE_PLACES_API_KEY`, `LEADGEN_API_KEY`.

## CLI

| Command | Purpose |
|---|---|
| `leads init` | Create config, `.env` and folders |
| `leads doctor` | Check config, credentials, sheet access, Places key, Overpass/Nominatim, DNS |
| `leads sheet init` | Create or upgrade tabs, headers, dropdowns and the Summary tab (safe to re-run) |
| `leads run <search>` / `--all` | Run configured searches |
| `leads run -k "law firm" -l "Leeds, UK" -s osm` | Ad-hoc search (`--osm-tag`, `--industry`, `--max-results`) |
| `leads add-urls sites.csv --location "Bristol, UK"` | Crawl websites you already have (txt or csv with a `website` column) |
| … `--dry-run --limit N --out preview.csv --force` | Preview without saving / cap the run / write CSV / ignore the recrawl window |
| `leads list -i "Legal Services" --country "United Kingdom" --status new` | Filter (`--format table/csv/json`, `--group-by industry`) |
| `leads status <lead_id/email/domain> contacted --note "intro sent"` | Update the pipeline status (logged to Activities) |
| `leads validate [--recheck]` | Re-validate stored emails |
| `leads export --email-status valid` | CSV/JSON for an email tool. Excludes invalid emails and opted-out leads by default |
| `leads forget <email/domain>` | GDPR erasure + permanent suppression |
| `leads purge [--older-than 365] [--dry-run]` | Retention clean-up |
| `leads scheduler start` / `list` | Run schedules in the foreground / show the next run times |
| `leads dashboard` | Streamlit UI at http://localhost:8501 |
| `leads api [--host H --port P]` / `--openapi FILE` | REST API with Swagger UI at http://localhost:8000/docs / write the OpenAPI schema |

Global options: `--config PATH` (or `LEADGEN_CONFIG`), `--store sheets|sqlite`, `-v` for debug logging.

## REST API (Swagger)

```bash
uv sync --extra api
uv run leads api                   # Swagger UI: http://localhost:8000/docs · ReDoc: /redoc · schema: /openapi.json
```

Swagger UI lists every endpoint with its parameters and response schemas, and **Try it out** sends real requests.

The same Swagger (OpenAPI 3.1) document is checked in twice, so you can read or share it without running the server:

- [`docs/swagger.yaml`](docs/swagger.yaml): readable. Open it in [editor.swagger.io](https://editor.swagger.io) (*File → Import file*) or a Swagger/OpenAPI viewer in VS Code.
- [`docs/openapi.json`](docs/openapi.json): for Postman (*Import*), client generators and API gateways.

Both are generated from the code. After changing the API, regenerate them (a test fails if either is stale):

```bash
uv run leads api --openapi docs/swagger.yaml && uv run leads api --openapi docs/openapi.json
```

| Endpoint | Purpose |
|---|---|
| `GET /leads` | Filter (`industry`, `country`, `city`, `status`, `email_status`, `source`, `q`; repeat a parameter to match any value) and paginate (`limit`, `offset`), newest first |
| `GET /leads/{lead_ref}` | One lead by `lead_id`, email or domain |
| `PATCH /leads/{lead_ref}` | CRM fields only: `status`, `append_note`, `notes`, `owner`, `tags`, `campaign`, `sequence_step`, `last_contacted_at`, `next_follow_up_at`. A status change is logged to Activities, as with `leads status` |
| `GET` / `POST /leads/{lead_ref}/activities` | Read / log events such as `email_sent`, `reply`, `call` |
| `GET /searches` | Searches from `config.yaml` with their schedules |
| `POST /runs` | Start a configured search in the background (`dry_run`, `limit`, `sources`, `force`). Returns `202` and a job |
| `GET /jobs/{job_id}` · `GET /jobs` | Progress of API-started runs (kept in memory until the server restarts) |
| `GET /runs` | Run history from all interfaces |
| `GET /export?format=csv\|json` | Download contactable leads (same filters; `include_opted_out=true` for everything) |
| `POST /privacy/forget` | GDPR erasure + suppression, like `leads forget` (**irreversible**) |
| `GET /health` | Liveness, no auth |

```bash
curl "localhost:8000/leads?status=new&email_status=valid&limit=20"
curl -X PATCH localhost:8000/leads/acme.com -H 'Content-Type: application/json' \
     -d '{"status": "contacted", "campaign": "q1-dental", "sequence_step": "1", "append_note": "intro sent"}'
curl -X POST localhost:8000/runs -H 'Content-Type: application/json' -d '{"search": "dental-uk", "dry_run": true}'
```

**Security.** By default the API listens on `127.0.0.1` only. To reach it from another machine, set `LEADGEN_API_KEY` in `.env` (e.g. `openssl rand -hex 32`); `leads api --host 0.0.0.0` refuses to start without it. Clients then send `X-API-Key: <key>` (in Swagger UI: **Authorize**). Use HTTPS (a reverse proxy) for anything beyond your own network: the API serves personal data.

## Scheduling

Either keep `uv run leads scheduler start` running (tmux, launchd, systemd), or let the OS run it, e.g. with cron:

```cron
0 6 * * MON  cd ~/lead-scraper && /opt/homebrew/bin/uv run leads run --all >> logs/cron.log 2>&1
```

The scheduler re-reads the config before each job, never overlaps the same search, and runs a missed job once when the machine wakes up.

## Data model and CRM integration

**Leads** (one row per company; columns are matched **by header name**, so you can reorder them or add your own):
`lead_id` (stable `ld_…` id) · `status` · `company_name` · `website` · `domain` · `email` · `email_status` (valid / risky / invalid / missing) · `email_type` (personal / role / free_mail) · `additional_emails` · `contact_name` · `contact_title` · `phone` (E.164) · `address` · `city` · `region` · `country` · `industry` · `linkedin_url` · `twitter_url` · `facebook_url` · `instagram_url` · `source` · `source_url` · `external_id` · `search_keyword` · `search_location` · `lawful_basis` · `tags` · `owner` · `notes` · `last_contacted_at` · `next_follow_up_at` · `campaign` · `sequence_step` · `run_id` · `created_at` · `updated_at`

**Status lifecycle** (dropdown in the sheet): `new → qualified → contacted → replied → meeting → won/lost`, plus `bounced`, `unsubscribed` and `do_not_contact`.

**Other tabs**: *Activities* (`lead_id, timestamp, type, details, campaign`, ready for email_sent / reply / call events), *Suppression* (hashed opt-outs), *Runs* (per-run stats and error notes), *Summary* (live `QUERY()` counts by industry, location, status, email quality and source).

**How a future cold-email / follow-up tool should plug in:**
1. Select leads with `status in (new, qualified)` and `email_status = valid` (optionally `risky`), and skip any whose email or domain hash is in *Suppression*.
2. After sending, set `status=contacted`, `last_contacted_at`, `campaign` and `sequence_step`, and append an *Activities* row.
3. On reply, bounce or unsubscribe, update `status` accordingly. Use `leads forget` for erasure requests.
4. Call the REST API (`leads api`, above), or use the `LeadStore` protocol (`src/leadgen/store/base.py`) as the integration point. A HubSpot, Notion or Postgres adapter only needs to implement it.

Also mention in your first email where you got the address and how to opt out (GDPR Art. 14).

## How the scraping works

Every run is one `asyncio` pipeline ([pipeline.py](src/leadgen/pipeline.py)). It starts by loading the existing leads and the suppression hashes from the store, so deduplication and opt-outs apply before anything is fetched. Then, for every *location × keyword × source*:

```
discover ─▶ pre-check ─▶ crawl site ─▶ extract ─▶ validate + rank emails ─▶ classify ─▶ final dedupe ─▶ batch write
(APIs)      (no HTTP)    (robots,      (HTML,      (DNS, no SMTP)            (industry)   (merge blanks)   (50 rows)
                         rate limit)   JSON-LD)
```

Discovery is streamed: each accepted business becomes its own task while the source is still yielding results. At most `2 × crawl.concurrency` businesses are in progress, and at most `crawl.concurrency` HTTP requests are in flight. An error in one business or one source is counted in the run stats (`errors`, `notes`), and the rest of the run continues.

### 1. Discovery: which businesses exist

Discovery uses official APIs and open data only. Each source yields `Candidate` records (name, website, email, phone, address, categories, `external_id`, `source_url`).

**OpenStreetMap** ([osm_overpass.py](src/leadgen/sources/osm_overpass.py)):

1. **Geocode** the location with Nominatim (`/search?q=…&format=jsonv2&limit=1&addressdetails=1`). Requests to Nominatim are at least 1.1 s apart (its policy allows 1/s), and results are cached in `data/state.db` for 90 days.
2. **Build an Overpass QL query.** The OSM tags come from `osm_tags`, or from the keyword's mapping in [industries.yaml](src/leadgen/data/industries.yaml). A city that geocodes to a boundary becomes an Overpass area (`3600000000 + relation id`, or `2400000000 + way id`). A city that geocodes to a point becomes an `around:` circle of radius `osm.point_radius_m` (10 km). Real queries the code generates for "marketing agency":

   ```
   [out:json][timeout:90];area(id:3600062422)->.a;
   (nwr["office"="advertising_agency"](area.a);nwr["office"="marketing"](area.a););out tags 50;
   ```

   ```
   [out:json][timeout:90];
   (nwr["office"="advertising_agency"](around:10000,55.6761,12.5683);nwr["office"="marketing"](around:10000,55.6761,12.5683););out tags 50;
   ```

   `nwr` matches nodes, ways and relations. `out tags N` returns only tags (no geometry) for at most `max_results` elements. A keyword with no tag mapping falls back to a case-insensitive name regex: `nwr["name"~"keyword",i]`.
3. **Map elements to candidates.** The name comes from `name` or `brand`; the website from `website`, `contact:website` or `url`; the email from `email` or `contact:email`; the phone from `phone`, `contact:phone` or `contact:mobile`. For multi-value tags (`a;b`), the first value is used. Elements with no name, or with no website, email or phone, are dropped. So a query can return fewer than `N` usable candidates. `external_id` is `osm:<type>/<id>`, and `source_url` is the element's openstreetmap.org page (ODbL attribution).

**Google Places API (New)** ([google_places.py](src/leadgen/sources/google_places.py)): `POST places:searchText` with `textQuery: "<keyword> in <location>"`. An `X-Goog-FieldMask` limits the response, and therefore the billing SKU, to the fields used. The source pages through results with `nextPageToken` (20 per page, 60 at most) and skips `CLOSED_PERMANENTLY` places. Requests to the API are at least 0.5 s apart. With `store_place_details: false`, candidates are marked *restricted*: only the place ID and Maps link are stored, and all other facts must come from the business's own website.

**Seed file** ([seed_urls.py](src/leadgen/sources/seed_urls.py)): `.txt` with one URL per line, or `.csv` with a `website`, `url` or `domain` column, plus optional `company_name`, `city`, `country` and `industry` columns.

### 2. Pre-check: cheap filters before any request to the business

- **URL normalization** ([normalize.py](src/leadgen/normalize.py)): defaults to `https://`, drops the fragment, lowercases the host, and rejects IP-address hosts.
- **Non-website hosts**: social networks, maps, review sites and link shorteners (facebook, instagram, linkedin, yelp, linktr.ee, bit.ly, …) are never crawled as a business site. A social URL is kept in the matching `*_url` column instead.
- **Registered domain**: computed with `tldextract` against a bundled, offline Public Suffix List snapshot (`www.acme.co.uk` → `acme.co.uk`). Subdomains of hosting platforms (`*.wixsite.com`, `*.vercel.app`, …) count as separate sites.
- **Suppression**: SHA-256 of `domain:<domain>` and `email:<address>` is checked against the Suppression list.
- **Early dedupe**: keys are `email:<address>`, `domain:<domain>`, and `name:<normalized company>|<normalized city>`. A company name alone is never a key. If any key matches an existing lead, the candidate is a duplicate. With `merge_duplicates: true`, its non-empty values fill only *blank* fields of the existing lead.
- **Recrawl window**: domains crawled within `recrawl_after_days` (30) are skipped. Override with `--force`.
- **Stable ID**: `lead_id = "ld_" + sha1(strongest key)[:12]`, where the domain is preferred, then the email, then name+city. The keys are reserved immediately, so the same business found by another source in the same run is skipped.

### 3. Fetching: the polite HTTP layer

All requests go through `PoliteFetcher` ([fetcher.py](src/leadgen/http/fetcher.py)), built on `httpx.AsyncClient`. It sends your `User-Agent`, an HTML-first `Accept` header and an English-first `Accept-Language` (`en;q=0.9,*;q=0.5`).

| Concern | Implementation |
|---|---|
| **robots.txt** ([robots.py](src/leadgen/http/robots.py)) | Parsed with [Protego](https://github.com/scrapy/protego) and cached per origin for 24 h. Groups are matched on the product token of your User-Agent (e.g. `TechnodexLeadBot`). `200` → obey the rules. `4xx` (except 429) → no robots.txt, so everything is allowed. `5xx`, `429`, a network error or an unparsable file → the whole site is treated as disallowed (RFC 9309). |
| **Redirects** | Followed manually, at most 5 hops. robots.txt is re-checked for every hop's URL, so a redirect can't lead into a disallowed path. Only `http(s)` is followed. |
| **Per-host rate limit** ([ratelimit.py](src/leadgen/http/ratelimit.py)) | A per-host `asyncio.Lock` allows one request in flight per host. The gap between requests is `max(per_domain_delay_s, robots Crawl-delay, Request-rate seconds/requests)`, capped at 60 s. |
| **Retries** | [tenacity](https://tenacity.readthedocs.io/): `crawl.retries` attempts in total, on `429/500/502/503/504` and transport errors. The wait honours `Retry-After` (seconds or HTTP date, ≤60 s); otherwise it backs off exponentially (`backoff_base_s × 2^(n-1)`, ≤30 s). |
| **Response limits** | Bodies are streamed. Non-HTML `Content-Type` and status `≥400` are discarded. The body is cut at `max_bytes` (2 MB) during download, then decoded with the declared charset (UTF-8 fallback, invalid bytes replaced). |
| **JS-only sites** (optional, [browser.py](src/leadgen/http/browser.py)) | With `render_js: true`, a page with fewer than 400 visible characters (after removing `<script>`, `<style>` and `<noscript>`) is re-rendered in headless Chromium (Playwright). The browser uses the same User-Agent and rate-limit slot, blocks images, media, fonts and CSS, and waits for `networkidle`. It only renders URLs already approved by robots.txt. |
| **APIs** (Nominatim, Overpass, Places) | The same rate limiter, concurrency cap and retries apply. robots.txt is not consulted, because these are documented APIs governed by their own usage policies. |

### 4. Website crawl: which pages are read

[website.py](src/leadgen/extract/website.py) fetches the homepage first. The final URL after redirects becomes the lead's `website` and `domain`. Links on each page are then ranked, and only matching ones are followed, up to `max_pages_per_site` pages in total, homepage included (default 6):

| Priority | URL path matches (case-insensitive) |
|---|---|
| 0 contact | `contact`, `kontakt`, `contacto`, `contatti`, `get-in-touch`, `reach-us`, `enquir` |
| 1 legal notice | `impressum`, `imprint`, `mentions-legales`, `legal-notice`, `aviso-legal` |
| 2 about | `about`, `ueber-uns`, `uber-uns`, `qui-sommes`, `chi-siamo`, `who-we-are`, `our-story`, `company` |
| 3 team | `team`, `staff`, `people`, `our-people`, `leadership`, `management`, `meet-` |

Ties go to the shorter URL. Only links on the same registered domain are followed. Files (`.pdf`, images, office documents, media) are never followed, and neither are `login`, `wp-admin`, `account`, `cart`, `checkout`, `feed`, `tag` or `category` paths, or `?replytocom=` / `?share=` links. A sub-page that redirects off-site is ignored, and a failing sub-page does not abort the rest of the site.

### 5. Extraction: what is read from each page

Pages are parsed with BeautifulSoup and `lxml`.

- **Emails** ([emails.py](src/leadgen/extract/emails.py)), from three places:
  1. `mailto:` links. They are URL-decoded, `?subject=…` is removed, and multiple recipients are split on `,`, `;` or whitespace.
  2. A regex over the page's *visible* text, after `<script>`, `<style>`, `<noscript>`, `<template>` and `<svg>` are removed.
  3. schema.org `email` fields (below).

  Every address is then cleaned. It is lowercased and trimmed, and it must contain exactly one `@`. Addresses are rejected if the "TLD" is a file extension (`logo@2x.png`), the domain is a placeholder (`example.com`, `sentry.io`, `wixpress.com`, …), the local part is a system or placeholder name (`noreply`, `postmaster`, `test`, `john.doe`, …) or a hex hash, or the RFC length limits are exceeded. Cloudflare `data-cfemail` and `name [at] domain [dot] com` obfuscation is *detected but never decoded*. The page where each address was first seen is recorded and becomes `source_url`.
- **schema.org JSON-LD** ([jsonld.py](src/leadgen/extract/jsonld.py)): every `<script type="application/ld+json">` block is parsed. A retry removes trailing commas if the first parse fails. The parser walks the whole tree, including `@graph`. A node counts as the organization if it has a `name` and an organization-like `@type` (`LocalBusiness`, `Dentist`, `…Agency`, …) or an address or telephone. From it the parser takes `name`/`legalName`, the `@type` list, `email`, `telephone`, `PostalAddress` (street, locality, region, postal code, country), `sameAs`, and `contactPoint` emails and phones. `Person` nodes become contact candidates (name of 5 words or fewer, with no digits, `@` or URLs, plus `jobTitle`). People under `author`, `creator`, `publisher`, `review` and similar keys are excluded, because they aren't contacts of the business.
- **Phones**: from `tel:` links and JSON-LD `telephone`. They are normalized to E.164 with Google's `phonenumbers` library, using the lead's country (or the search location's country) as the default region. A number that isn't a *possible* number for that region is kept as cleaned raw text if it has at least 6 digits; otherwise it is dropped.
- **Social profiles** ([social.py](src/leadgen/extract/social.py)): from every link and from `sameAs`. Links are classified as Facebook, Instagram, Twitter/X, or LinkedIn company vs. person. Share, intent and plugin URLs, reserved paths, and website-builder template handles (`wix`, `squarespace`, …) are ignored. Personal LinkedIn URLs are kept only with `personal_data: allow`.
- **Company name**: the first available of the source's name, the JSON-LD organization name, `og:site_name`, or the first segment of `<title>` split on `| - – — : · •` (generic titles such as "Home" or "Welcome" are skipped).
- **Address and location**: the source first, then JSON-LD `PostalAddress`, then the search location (`"City, Country"`) as a fallback for city and country.

### 6. Email validation and selection

[validate/email.py](src/leadgen/validate/email.py) checks each address in this order. **No SMTP mailbox probing** is done: it is unreliable and gets IPs blacklisted.

| Check | Result |
|---|---|
| Syntax (`email-validator`, RFC 5321/5322) fails | `invalid` |
| Domain on the bundled disposable-domain list | `invalid` |
| DNS: NXDOMAIN, null MX (RFC 7505), or no records at all | `invalid` |
| DNS: timeout or server failure | `risky` |
| DNS: no MX, but an A record (implicit MX) | `risky` |
| Free-mail provider (gmail.com, outlook.com, …) | `risky` |
| Email domain differs from the website's domain | `risky` |
| Otherwise | `valid` |

MX lookups use `dnspython` with a 6 s timeout and are cached per domain for the whole run. So `valid` means "a deliverable domain that matches the business", not "mailbox confirmed". For guaranteed deliverability, plug a paid verifier into `EmailValidator.check()`.

Each address is also classified as `role` (shared, e.g. `info@`, `sales@`, or any `.`/`-`/`_`-separated token that is a department or list word), `free_mail`, or `personal`. This is a heuristic: acronym mailboxes like `psf@` or `coc@` can't be told apart from a person's name.

**Choosing the primary email.** Invalid addresses are dropped, and the rest are scored:

| Factor | Points |
|---|---|
| `valid` / `risky` | 100 / 50 |
| Domain matches the website | +20 |
| `personal` (only with `personal_data: allow`) | +10 |
| `role` | +5 |
| Low-value mailbox (careers, privacy, support, billing, press, newsletter, …) | −30 |

The highest score becomes `email`, and the next five go to `additional_emails`. With `personal_data: role_only`, only role addresses are kept, and no contact names are stored.

**Contact person.** The first match wins:

1. The JSON-LD person whose email is the primary email.
2. For a personal email, a person whose first or last name appears in the local part.
3. The first person whose title matches `owner|founder|director|ceo|chief|principal|managing|manager|partner|head|president|inhaber|geschäftsführer|gerente|directeur…`.
4. The only person, if just one is listed.

### 7. Classification, final dedupe and storage

- **Industry** ([industry.py](src/leadgen/extract/industry.py)): the first available of the search's `industry` override, the source categories (OSM tag such as `office=advertising_agency`, or Places type), the website's schema.org `@type`, or the search keyword, each mapped through the taxonomy. An unknown category is humanized rather than lost.
- **Keep or drop**: a lead is dropped if it has no email, website or phone; if `require_email` is set and no email was found; or if it came from a restricted source (Places) and its own website yielded nothing.
- **Final dedupe**: the keys are recomputed from the enriched lead, because the crawl may have found a different domain after redirects or a new email. A match merges *blank* fields only. Pipeline merges never overwrite user-owned columns (`status`, `owner`, `notes`, `tags`, `campaign`, `sequence_step`, follow-up dates).
- **Writes**: new leads are buffered and written 50 at a time, in a worker thread so the event loop isn't blocked. If a write fails, the batch is appended to `data/unsaved_<run_id>.csv` and noted in the run, so nothing is lost. Each crawled domain is recorded in `data/state.db` (except on dry runs), and the run's stats are logged to the Runs tab or table.

### Limitations

- Businesses that publish only a contact form, or hide their address, produce leads without an email (`email_status = missing`).
- Without `render_js: true`, sites that build their content entirely in JavaScript yield almost nothing.
- OSM coverage and tagging vary by country and city. `out tags N` returns the first `N` matching elements, not the "best" ones.
- Email validation is DNS-based only. Run `leads validate --recheck` before a campaign, and consider a paid verifier.

## Extending

- **New source** (a directory you've confirmed allows automated access, a search API, a CSV feed): implement `Source` in `src/leadgen/sources/base.py` and register it in `sources/registry.py`.
- **Enrichment** (e.g. Hunter.io for named contacts): implement `Enricher` in `src/leadgen/enrich/base.py` and pass it to `Pipeline(enrichers=[...])`.
- **JS-only sites**: run `uv sync --extra browser && uv run playwright install chromium`, then set `crawl.render_js: true`. Only pages that look empty are rendered.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Overpass HTTP 406/403 … refused this client` | The public Overpass server filters some clients. Read its [usage policy](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html), make sure `user_agent` names your application with a real contact URL/email, or set `osm.overpass_url` to another instance you may use (e.g. self-hosted). |
| `Google Places API 403` | Enable *Places API (New)* (not the legacy Places API), enable billing, and check the key restrictions. |
| Sheets `PERMISSION_DENIED` / `SpreadsheetNotFound` | Share the sheet with the service account email as Editor; check `GOOGLE_SHEET_ID`. |
| Store write failed mid-run | Nothing is lost: the batch is saved to `data/unsaved_<run_id>.csv` and noted in the Runs tab. |
| Few emails found | Many small businesses only publish a contact form. Try more keywords or Places, or add an enricher. `require_email: true` drops email-less leads. |
| Summary tab broken after reordering columns | Run `leads sheet init` again. |

Logs: the console, plus JSON lines in `logs/leadgen.log` (rotated).

## Development

```bash
uv sync --extra dashboard --extra api   # the dashboard and API tests are skipped without these
uv run pytest            # 75 tests: extractors, validation, robots/rate limits (mocked HTTP), sources,
                         # end-to-end pipeline (SQLite), Sheets store (fake gspread), CLI, REST API, dashboard
uv run ruff check src tests && uv run ruff format src tests
```

Data from OpenStreetMap is © OpenStreetMap contributors (ODbL). Keep the `source_url` / `external_id` attribution.
