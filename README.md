# leadgen: compliant B2B lead discovery into PostgreSQL or Google Sheets

`leadgen` finds businesses that match your keywords and locations, then reads the **public contact pages on each business's own website**. It extracts the company name, email, contact person, phone, address, industry and social profiles, validates the emails, removes duplicates, and stores one row per company in **Google Sheets**, **PostgreSQL** or a local SQLite file. The sheet is structured so a cold-email or CRM workflow can plug in later.

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

## Quick start: run the project

Requires [uv](https://docs.astral.sh/uv/) (Python 3.11+; tested on 3.13). Run every command from the project folder (`cd ~/lead-scraper`).

### 1. Install (once, and after every update)

```bash
uv sync --extra dashboard --extra api --extra postgres
```

> **Always name all three extras.** `uv sync` *removes* every extra you leave out, so `uv sync --extra dashboard` alone uninstalls the PostgreSQL and API packages, and every `leads` command then fails with "needs the postgres extra". Add `--extra browser` only if you enable JavaScript rendering.

### 2. Set up (first time only)

```bash
uv run leads init        # creates config/config.yaml, .env and the data/ logs/ secrets/ exports/ folders
                         # (existing files are never overwritten)
```

1. In **`config/config.yaml`**: set `user_agent` to your bot name and a real contact, set `store: postgres`, and define your `searches`.
2. In **`.env`**: set `DATABASE_URL` (see [PostgreSQL setup](#postgresql-setup-recommended-for-production)).
3. Create the database tables, then check the setup:

```bash
uv run leads db migrate  # create/upgrade the tables (on Neon: see "direct URL" in PostgreSQL setup)
uv run leads doctor      # `config`, `user agent` and `postgres` must say ok
```

Not using PostgreSQL? Set `store: sqlite` (a local file, nothing to configure), or `store: sheets` and follow [Google Sheets setup](#google-sheets-setup-free), then run `uv run leads sheet init`.

### 3. Run

Each long-running command needs its own terminal. Stop it with **Ctrl+C**.

```bash
# Apps
uv run leads dashboard                            # web UI            → http://localhost:8501
uv run leads api                                  # REST API + Swagger → http://localhost:8000/docs

# Collect leads
uv run leads run dental-uk --dry-run --limit 10   # preview: crawls a few sites, saves nothing
uv run leads run dental-uk                        # real run: saves new leads to the database
uv run leads run --all                            # every search in config.yaml
uv run leads scheduler start                      # run searches on their schedules (keep it open)

# Work with the leads
uv run leads list --status new --email-status valid
uv run leads status info@acme.com contacted --note "intro sent"
uv run leads export --email-status valid          # CSV for an email tool → exports/
```

All commands: [CLI](#cli). Day-to-day order of steps: [Workflow](#workflow).

**`leads doctor` lines that may say FAIL without stopping you:**

- `google places`: no `GOOGLE_PLACES_API_KEY`. It's optional; searches that list `google_places` just skip it.
- `overpass`: the public OpenStreetMap server is slow or busy. Runs may still work. See [Troubleshooting](#troubleshooting).

### PostgreSQL setup (recommended for production)

1. Get a PostgreSQL 16+ database: a managed service such as [Neon](https://neon.com) (this project's choice; free tier), a local install (e.g. Homebrew), or `docker compose up -d` with the included [docker-compose.yml](docker-compose.yml).
2. Install the `postgres` extra together with the others you use (`uv sync --extra dashboard --extra api --extra postgres`; `uv sync` removes extras you leave out), and put the connection string in `.env` as `DATABASE_URL` (e.g. `postgresql://user:password@localhost:5432/leadgen`).
3. `uv run leads db migrate` creates the tables (run it again after upgrading leadgen). On Neon, keep the **pooled** URL (`-pooler` in the host) in `.env` for the app, and run migrations over the **direct** URL: `DATABASE_URL="<URL without -pooler>" uv run leads db migrate`.
4. Set `store: postgres` in `config/config.yaml`. Moving existing data? `uv run leads db import --from sqlite` (or `--from sheets`, or `--from postgres` with the source in `SOURCE_DATABASE_URL`, e.g. local → Neon; `--dry-run` first) copies leads, activities, opt-outs and run history, and is safe to re-run.

Why, and what comes next: [docs/postgres-migration-plan.md](docs/postgres-migration-plan.md).

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

## Workflow

From setup to closed deal. Steps 4–10 repeat for every campaign or schedule. The CLI, the dashboard, the REST API and the Google Sheet all read and write the same store, so you can mix them.

```
  1. Set up ......... uv sync (3 extras) → leads init → config.yaml + .env → leads db migrate → leads doctor   (once)
        │
  2. Define ......... searches in config.yaml: keywords × locations × sources
        │
  3. Preview ........ leads run <search> --dry-run --limit 20            nothing is saved
        │
  4. Collect ........ leads run <search> · scheduler · dashboard · POST /runs
        │               → new leads, status = new
  5. Review ......... dashboard · Google Sheet · leads list · GET /leads
        │               → qualified  or  do_not_contact
  6. Re-check ....... leads validate --recheck                            emails go stale
        │
  7. Export ......... leads export · GET /export                          contactable leads only
        │
  8. Outreach ....... your email tool → status contacted + Activities row (email_sent)
        │
  9. Follow up ...... replied → meeting → won / lost        bounced · unsubscribed → never contacted again
        │
 10. Maintain ....... leads forget (erasure) · leads purge (retention) · next scheduled run → step 4
```

**Lead status lifecycle.** Opt-out statuses are left out of every export, and `leads purge` turns them into suppression hashes before deleting the lead:

```
 run ──▶ new ──▶ qualified ──▶ contacted ──▶ replied ──▶ meeting ──▶ won / lost
          │                        │
          ▼                        ▼
    do_not_contact         bounced · unsubscribed         (opt-out statuses)
```

### Step by step

1. **Set up (once).** Follow [Quick start](#quick-start-run-the-project): install, run `leads init`, set `user_agent` to your real bot name and contact (public OpenStreetMap services may refuse unidentified clients), add credentials to `.env`, run `leads db migrate` (PostgreSQL) or `leads sheet init` (Google Sheets), and repeat `leads doctor` until its checks pass.

2. **Define searches** in [`config/config.yaml`](#configuration-configconfigyaml): `keywords × locations × sources`.
   - Use cities rather than whole countries. `max_results` applies per location, and country-wide Overpass queries are slow.
   - Check that each keyword maps to OSM tags in the [industry taxonomy](src/leadgen/data/industries.yaml), or set `osm_tags`. Keywords that map to the same tags only repeat the query.
   - `leads scheduler list` or `GET /searches` shows what is configured.

3. **Preview.** `leads run <search> --dry-run --limit 20 --out preview.csv` discovers and crawls but saves nothing. It also doesn't mark domains as crawled, so the real run still visits them. The dashboard's *Run search* tab and `POST /runs` with `"dry_run": true` do the same.

4. **Collect.** `leads run <search>` (or `--all`); the dashboard's *Run search* tab with *Dry run* unticked; `POST /runs`; or schedules (`leads scheduler start`, or cron). Each run discovers businesses, crawls their sites, validates emails, removes duplicates, and writes new leads with `status = new`. Per-run stats go to the *Runs* tab or table (`GET /runs`). Re-running is safe:
   - known businesses only get their blank fields filled;
   - domains crawled in the last 30 days are skipped;
   - suppressed emails and domains are never collected again.

   How a run works internally is described in [How the scraping works](#how-the-scraping-works).

5. **Review and qualify.** Open the dashboard's *Leads* tab, the Google Sheet (status dropdown), `leads list --status new --email-status valid`, or `GET /leads?status=new`. Mark good fits `qualified` and bad fits `do_not_contact`, and set `owner`, `notes` and `next_follow_up_at` as needed. The CLI, dashboard and API log every status change to *Activities*; edits typed directly into the Google Sheet are not logged.

6. **Re-check emails before sending.** `leads validate --recheck` repeats the DNS checks for every stored email; without `--recheck`, only risky or unchecked emails are re-checked. Domains expire and mail servers move, so do this before each campaign.

7. **Export.** `leads export --status qualified --email-status valid` writes `exports/leads_<timestamp>.csv` (add `--format json` for JSON), or call `GET /export?status=qualified&email_status=valid`. Leads without an email, with an `invalid` email, or with an opt-out status are excluded by default. The dashboard's *Download CSV* button exports the filtered table **as is**, opt-outs included, so use `leads export` for email tools.

8. **Outreach.** Send with your email tool. Say where you found the address and how to opt out (GDPR Art. 14), and check the cold-email rules of the recipient's country. Then record what you sent:

   ```bash
   leads status info@acme.com contacted --note "intro email, sequence step 1"
   # or, from an email tool, via the API:
   curl -X PATCH localhost:8000/leads/info@acme.com -H 'Content-Type: application/json' \
        -d '{"status": "contacted", "campaign": "q1-dental", "sequence_step": "1"}'
   curl -X POST localhost:8000/leads/info@acme.com/activities -H 'Content-Type: application/json' \
        -d '{"type": "email_sent", "details": "intro email", "campaign": "q1-dental"}'
   ```

   Setting `contacted` also stamps `last_contacted_at`.

9. **Follow up.** Move leads through `replied → meeting → won / lost`, and use `next_follow_up_at` to plan the next touch. A bounce becomes `bounced`, and an opt-out becomes `unsubscribed` or `do_not_contact`. These three statuses are excluded from every export.

10. **Maintain.**
    - **Erasure requests:** `leads forget jane@acme.com` (one person) or `leads forget acme.com` (whole company), or `POST /privacy/forget`. The data is deleted and a SHA-256 hash is kept, so it is never collected or contacted again.
    - **Retention:** `leads purge --dry-run`, then `leads purge`. This deletes leads older than `retention_days` that were never engaged; opt-outs become suppression hashes first.
    - Scheduled runs start again at step 4. They skip everything already known or suppressed.

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
- **Reuse a city list** across searches with a YAML anchor: name it once (`locations: &english_markets` followed by the list) and reuse it with `locations: *english_markets`. `config/config.yaml` does this for its industry searches. Restart the dashboard and API after editing the config, so they pick up the change.
- **OSM tags**: if a keyword has no mapping, the OSM source falls back to a business-name search. Set `osm_tags` for precise results (see the [OSM Map Features](https://wiki.openstreetmap.org/wiki/Map_features) wiki).
- **Store**: `store: sheets | sqlite | postgres`. PostgreSQL is the choice for production and for several programs writing at once; SQLite for local use and tests; Sheets as a shared spreadsheet.
- Secrets live only in `.env`: `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEET_ID`, `GOOGLE_PLACES_API_KEY`, `LEADGEN_API_KEY`, `DATABASE_URL`.

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
| `leads db migrate [--sql]` | Create or upgrade the PostgreSQL schema (`--sql` prints it for review) |
| `leads db status` | PostgreSQL server, schema version and row counts |
| `leads db import --from sqlite\|sheets\|postgres [--path FILE] [--dry-run]` | Copy everything into PostgreSQL (`DATABASE_URL`); `--from postgres` reads `SOURCE_DATABASE_URL`. Checks values first, safe to re-run |

Global options: `--config PATH` (or `LEADGEN_CONFIG`), `--store sheets|sqlite|postgres`, `-v` for debug logging.

## REST API (Swagger)

```bash
uv sync --extra dashboard --extra api --extra postgres   # the extras you use; uv sync removes unlisted ones
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

**PostgreSQL** (`store: postgres`) is the store for email automation: several programs can write at once (sender workers, provider webhooks), and the database enforces one lead per domain and per email, even across concurrent runs. Status of the migration and the email-automation groundwork: [docs/postgres-migration-plan.md](docs/postgres-migration-plan.md).

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

## Project structure

Each file, with one line on what it's for. Folders marked *(local)* are created on your machine and kept out of git.

```
lead-scraper/
├── README.md                      # this guide
├── pyproject.toml                 # dependencies, optional extras (dashboard, api, postgres, browser), `leads` command, pytest/ruff settings
├── uv.lock                        # exact dependency versions, for reproducible installs
├── .gitignore                     # keeps secrets, databases, logs and exports out of git
├── .env                           # (local) secrets: Google credentials, Places API key, LEADGEN_API_KEY, DATABASE_URL
├── docker-compose.yml             # local PostgreSQL for development and tests (`docker compose up -d`)
├── config/
│   └── config.yaml                # your searches, schedules, crawl, OSM and compliance settings
├── docs/
│   ├── swagger.yaml               # REST API spec (OpenAPI 3.1), readable; generated by `leads api --openapi`
│   ├── openapi.json               # the same spec as JSON, for Postman and client generators
│   └── postgres-migration-plan.md # phased plan to move storage to PostgreSQL before email automation
├── data/                          # (local) leads.db (SQLite leads), state.db (crawl history + geocode cache), backup/, unsaved_*.csv
├── logs/                          # (local) leadgen.log: rotating JSON-lines log of every run
├── exports/                       # (local) CSV/JSON files written by `leads export`
├── secrets/                       # (local) service-account.json for Google Sheets
│
├── src/leadgen/
│   ├── __init__.py                # package version
│   ├── cli.py                     # the `leads` command line (Typer): init, doctor, run, list, status, export, forget, api …
│   ├── api.py                     # REST API (FastAPI) with Swagger UI: leads, activities, runs, export, GDPR erasure
│   ├── config.py                  # loads and validates config.yaml and .env secrets (Pydantic)
│   ├── models.py                  # shared data types: Lead (one sheet row), statuses, Candidate, RunStats
│   ├── runner.py                  # one entry point for CLI, API, dashboard and scheduler: builds sources, runs the pipeline
│   ├── pipeline.py                # a run: discover → pre-check → crawl → extract → validate → dedupe → batch write
│   ├── dedupe.py                  # duplicate keys (email / domain / name+city) and stable `ld_…` lead IDs
│   ├── normalize.py               # cleans URLs, domains (Public Suffix List), phones (E.164), countries and names
│   ├── compliance.py              # suppression hashes, GDPR erasure (`forget`) and retention clean-up (`purge`)
│   ├── views.py                   # lead filters, counts and status-change helpers shared by CLI, dashboard and API
│   ├── scheduler.py               # runs configured searches on cron schedules (APScheduler)
│   ├── logging_setup.py           # readable console output plus the rotating JSON-lines log file
│   │
│   ├── sources/                   # DISCOVERY: where businesses come from
│   │   ├── base.py                # Source interface: (keyword, location) → candidate businesses
│   │   ├── registry.py            # creates sources by name; register new sources here
│   │   ├── osm_overpass.py        # OpenStreetMap: geocode with Nominatim, query businesses with Overpass
│   │   ├── google_places.py       # Google Places API (New) text search; discovery-only by default
│   │   └── seed_urls.py           # your own list of websites (.txt or .csv)
│   │
│   ├── http/                      # FETCHING: polite HTTP
│   │   ├── fetcher.py             # every request: robots.txt, rate limit, retries, redirects, 2 MB cap
│   │   ├── robots.py              # robots.txt download, cache and rules (Protego, RFC 9309)
│   │   ├── ratelimit.py           # one request at a time per site, with a minimum gap
│   │   └── browser.py             # optional headless Chromium (Playwright) for JavaScript-only pages
│   │
│   ├── extract/                   # EXTRACTION: reading business websites
│   │   ├── website.py             # crawls homepage + contact/legal/about/team pages, collects everything found
│   │   ├── emails.py              # emails from mailto links and visible text; drops junk; never decodes hidden ones
│   │   ├── jsonld.py              # schema.org JSON-LD: organization name, address, phones, emails, people
│   │   ├── social.py              # recognizes Facebook, Instagram, X/Twitter and LinkedIn profile links
│   │   └── industry.py            # maps OSM tags, Places types, schema.org types and keywords to one industry
│   │
│   ├── validate/
│   │   └── email.py               # email checks (syntax, disposable, DNS MX), role/personal type, best-email ranking
│   ├── enrich/
│   │   └── base.py                # hook for optional extra data sources (e.g. an official API); none built in
│   │
│   ├── store/                     # STORAGE: where leads are saved
│   │   ├── base.py                # LeadStore interface that every storage backend implements
│   │   ├── sheets.py              # Google Sheets backend: tabs, headers, dropdowns, Summary formulas, safe RAW writes
│   │   ├── sqlite_store.py        # SQLite backend: same interface, one local file
│   │   ├── postgres_store.py      # PostgreSQL backend: connection pool, constraints, timestamp conversion
│   │   ├── migrate.py             # runs the Alembic schema migrations for `leads db migrate`
│   │   ├── migrations/            # Alembic environment + versions/0001_initial.py (the PostgreSQL schema)
│   │   ├── transfer.py            # `leads db import`: checked, re-runnable copy from SQLite/Sheets into PostgreSQL
│   │   └── state.py               # crawl state, not leads: when each domain was crawled, cached geocodes
│   │
│   ├── dashboard/
│   │   └── app.py                 # Streamlit UI: KPIs, charts, editable leads table, run a search, run history
│   │
│   └── data/                      # files shipped inside the package
│       ├── config.example.yaml    # template copied to config/config.yaml by `leads init`
│       ├── env.example            # template copied to .env by `leads init`
│       ├── industries.yaml        # industry taxonomy: ~30 industries with keywords, OSM tags, Places/schema.org types
│       └── disposable_domains.txt # throwaway-email domains, marked invalid during validation
│
└── tests/                         # pytest suite: no real network, DNS or Google calls
    ├── conftest.py                # shared fixtures: test config, fake DNS, HTML loader, throwaway PostgreSQL schema
    ├── fixtures/acme_*.html       # sample business pages (home, contact) used by extraction and pipeline tests
    ├── test_extract.py            # email, JSON-LD, social-link and website parsing
    ├── test_fetcher.py            # robots.txt rules, crawl-delay, retries, redirects, size cap (mocked HTTP)
    ├── test_sources.py            # OSM, Places and seed-file sources against mocked APIs
    ├── test_pipeline.py           # end-to-end runs into SQLite: dedupe, merges, dry run, opt-outs, error isolation
    ├── test_dedupe_validate.py    # duplicate keys, lead IDs, email validation, suppression hashes
    ├── test_sheets_store.py       # Google Sheets backend against a fake gspread
    ├── test_store_contract.py     # identical behaviour from SQLite, Sheets and PostgreSQL backends
    ├── test_postgres.py           # PostgreSQL constraints, schema checks, `leads db` commands, import, API
    ├── test_compliance_cli.py     # CLI commands, purge and forget
    ├── test_dashboard.py          # renders the dashboard headlessly
    └── test_api.py                # REST API endpoints, API key, background runs, Swagger file freshness
```

## Extending

- **New source** (a directory you've confirmed allows automated access, a search API, a CSV feed): implement `Source` in `src/leadgen/sources/base.py` and register it in `sources/registry.py`.
- **Enrichment** (e.g. Hunter.io for named contacts): implement `Enricher` in `src/leadgen/enrich/base.py` and pass it to `Pipeline(enrichers=[...])`.
- **JS-only sites**: add `--extra browser` to your usual `uv sync` command (e.g. `uv sync --extra dashboard --extra api --extra postgres --extra browser`), run `uv run playwright install chromium`, then set `crawl.render_js: true`. Only pages that look empty are rendered.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Overpass … ReadTimeout` / `The read operation timed out` | The public Overpass server is busy (it happens with big cities). Retry later, or set `osm.overpass_url` to another public instance listed on the [OSM wiki](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances). Other locations in the same run are unaffected. |
| `Port 8501 (or 8000) is already in use` | A dashboard (or API) is already running: open its URL, stop it with Ctrl+C in its terminal, or start another with `--port 8502`. `lsof -i :8501` shows what holds the port. |
| `PostgreSQL store needs: uv sync --extra postgres …` | An install removed the extra: run `uv sync --extra dashboard --extra api --extra postgres`. |
| `Overpass HTTP 406/403 … refused this client` | The public Overpass server filters some clients. Read its [usage policy](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html), make sure `user_agent` names your application with a real contact URL/email, or set `osm.overpass_url` to another instance you may use (e.g. self-hosted). |
| `Google Places API 403` | Enable *Places API (New)* (not the legacy Places API), enable billing, and check the key restrictions. |
| Sheets `PERMISSION_DENIED` / `SpreadsheetNotFound` | Share the sheet with the service account email as Editor; check `GOOGLE_SHEET_ID`. |
| Store write failed mid-run | Nothing is lost: the batch is saved to `data/unsaved_<run_id>.csv` and noted in the Runs tab. |
| Few emails found | Many small businesses only publish a contact form. Try more keywords or Places, or add an enricher. `require_email: true` drops email-less leads. |
| Summary tab broken after reordering columns | Run `leads sheet init` again. |
| `PostgreSQL schema is at …, latest is …` | Run `leads db migrate` (after installing or upgrading leadgen). |
| `SSL connection has been closed unexpectedly` (dashboard or API on Neon) | Neon closes idle connections when it pauses after 5 idle minutes. The store now checks each pooled connection before use and replaces dead ones; if you still see this, restart the dashboard or API so it loads the current code. |
| `cannot connect to PostgreSQL` | Check `DATABASE_URL` in `.env`, that the server is running (`pg_isready`), and that the database exists (`createdb leadgen`). Hosted databases need `?sslmode=require`; Neon's connection strings include it. |

Logs: the console, plus JSON lines in `logs/leadgen.log` (rotated).

## Development

```bash
uv sync --extra dashboard --extra api --extra postgres   # tests for missing extras are skipped
uv run pytest            # extractors, validation, robots/rate limits (mocked HTTP), sources, end-to-end pipeline,
                         # store contract (SQLite, Sheets, PostgreSQL), CLI, REST API, dashboard
TEST_DATABASE_URL=postgresql:///leadgen_test uv run pytest   # also run every PostgreSQL test (125 in total);
                         # each test gets its own throwaway schema in that database
uv run ruff check src tests && uv run ruff format src tests
```

Data from OpenStreetMap is © OpenStreetMap contributors (ODbL). Keep the `source_url` / `external_id` attribution.
