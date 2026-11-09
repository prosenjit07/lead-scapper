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

Interfaces: **CLI** (`leads …`), **Streamlit dashboard** (`leads dashboard`), **scheduler** (`leads scheduler start`, or cron).

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
- Secrets live only in `.env`: `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEET_ID`, `GOOGLE_PLACES_API_KEY`.

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

Global options: `--config PATH` (or `LEADGEN_CONFIG`), `--store sheets|sqlite`, `-v` for debug logging.

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
4. The `LeadStore` protocol (`src/leadgen/store/base.py`) is the integration point. A HubSpot, Notion or Postgres adapter only needs to implement it.

Also mention in your first email where you got the address and how to opt out (GDPR Art. 14).

## Email validation

Checks syntax (RFC), disposable domains, and MX / null-MX via DNS (cached per domain). It also classifies addresses as personal, role or free-mail and checks whether the domain matches the website. **No SMTP mailbox probing** (it is unreliable and gets IPs blacklisted). `valid` therefore means "deliverable domain that matches the business", not "mailbox confirmed". For guaranteed deliverability, plug a paid verifier into `EmailValidator.check()`.

The personal-vs-role split is a heuristic. Department and list words (info, sales, security, newsletter, …) mark an address as shared, but acronym mailboxes like `psf@` or `coc@` can't be told apart from a person's name.

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
uv run pytest            # 58 tests: extractors, validation, robots/rate limits (mocked HTTP), sources,
                         # end-to-end pipeline (SQLite), Sheets store (fake gspread), CLI, dashboard
uv run ruff check src tests && uv run ruff format src tests
```

Data from OpenStreetMap is © OpenStreetMap contributors (ODbL). Keep the `source_url` / `external_id` attribution.
