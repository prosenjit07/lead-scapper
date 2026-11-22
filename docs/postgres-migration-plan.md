# PostgreSQL migration plan

**Status:** Phases 0–2 done, and this project runs on PostgreSQL (2026-11-22). **Next:** Phase 3. · **Scope:** storage layer only. The scraping pipeline does not change.

## Progress

| Phase | Status | Notes |
|---|---|---|
| 0. Prerequisites | Done | Both data bugs fixed, with regression tests. The two affected rows were corrected in SQLite before the import (backup in `data/backup/`). `docker-compose.yml` added. Development uses the local Homebrew PostgreSQL 18.3; Docker isn't installed on this machine. Hosting is still open (section 10). |
| 1. `PostgresStore` | Done | See *Delivered* under Phase 1. 124 tests pass with `TEST_DATABASE_URL`; 95 pass and 29 PostgreSQL tests skip without it. |
| 2. Move the data | Done | `leads db import --from sqlite`: 10 leads and 3 runs copied; all fields identical when compared lead by lead; no rejected or duplicate rows. `config.yaml` now has `store: postgres`. |
| 3. Query methods | Not started | |
| 4. Crawl state | Not started | Optional; `data/state.db` is still SQLite. |
| 5. Email automation | Not started | Waits for the email-provider decision. |

## 1. Goal

Make PostgreSQL the system of record for leads, so that the planned **email automation** (sending workers, email-provider webhooks) and **lead tracking** can run concurrently, on a server, without losing or duplicating data.

- **SQLite stays** for local development and the test suite.
- **Google Sheets stays optional**, as a view or export for people. It is not suitable as the database behind automated sending, because it has per-minute API quotas and no transactions.

**Not in scope:** rewriting business logic with an ORM, changing how scraping works, and building the email automation itself. Phase 5 (section 6) only reserves its schema and rules, so the design doesn't have to change later.

## 2. Why PostgreSQL

The reason isn't data volume: SQLite handles millions of rows. The reason is **concurrent writers**, which arrive with email automation:

| Need | SQLite today | PostgreSQL |
|---|---|---|
| Scraper run, email sender, webhook receiver, API and dashboard writing at the same time | One writer at a time; others fail with `database is locked` after 5 s | Many concurrent writers |
| Two sending workers never send the same email | Hard to do safely | `SELECT … FOR UPDATE SKIP LOCKED` |
| Run on a server, with workers on other machines | A local file | Network database, hosted options |
| One lead per domain or email, valid statuses | Not enforced (all columns `TEXT`, one index) | Unique and check constraints |
| Fast tracking queries (due follow-ups, campaign reply rates) | Everything is filtered in Python | Indexed SQL |
| Backups, user permissions, encrypted connections (personal data) | Copy a file | Point-in-time recovery, roles, TLS |

## 3. Current state: what has to change

| Area | Today | Where |
|---|---|---|
| Storage interface | 12-method `LeadStore` protocol; backends `SheetsStore` and `SQLiteStore` | `src/leadgen/store/base.py`, `sheets.py`, `sqlite_store.py` |
| Backend choice | `store: sheets \| sqlite` | `config.py` (`AppConfig.store`), `cli.py` (`main`, `--store` check), `store/__init__.py` (`open_store`), `dashboard/app.py` (`--store` / `LEADGEN_STORE`) |
| **Reads** | Every reader calls `load_leads()` (**all** rows) and filters in Python with `views.filter_leads` / `find_lead` | `pipeline.Pipeline.run`, `pipeline.revalidate`, `api._get_lead`, `api.list_leads`, `api.export`, `cli._filtered`, `cli.status`, `compliance.forget`, `compliance.purge`, `dashboard/app.load_df` |
| Data types | Every `Lead` field is a `str`, with `""` meaning unknown (a Sheets constraint) | `models.Lead` |
| Concurrency | The API serializes all store access with one lock | `api.ApiState.store_lock` |
| Crawl state | Separate SQLite file `data/state.db` (last crawl per domain, geocode cache) | `store/state.py`, created in `runner.py`, `api.py`, `cli.py` |
| Schema changes | SQLite adds missing columns at startup; no versioning | `SQLiteStore.setup` |
| Duplicate protection | In-memory index per run, so **two concurrent runs can insert the same business twice** | `dedupe.DedupeIndex` |

## 4. Target architecture

```
 CLI · REST API · dashboard · scheduler · (later) email workers · webhook receiver
                                │
                    LeadStore protocol (+ query methods, Phase 3)
               ┌────────────────┼──────────────────┐
         PostgresStore      SQLiteStore         SheetsStore
         (production)       (dev, tests)        (optional view / export)
               │
         PostgreSQL ≥ 16  ◀── Alembic migrations (`leads db migrate`)
```

### Decisions

| Topic | Choice | Reason |
|---|---|---|
| Driver | `psycopg` 3 with `psycopg_pool` | Maintained, sync + async, server-side parameters, connection pool |
| Data access | Plain SQL inside `PostgresStore`, no ORM | The project already has its own models (`Lead`, `RunStats`); the store stays small and explicit |
| Migrations | Alembic, with raw SQL (`op.execute`) | Versioned upgrades and downgrades, a standard tool, no ORM models needed |
| Configuration | `store: postgres` in `config.yaml`; `DATABASE_URL` in `.env` | Secrets stay in `.env`, like the other credentials |
| Packaging | New optional extra: `postgres = ["psycopg[binary,pool]>=3.2", "alembic>=1.13", "sqlalchemy>=2.0"]` | SQLite and Sheets users don't install it |
| Local database | `docker-compose.yml` with `postgres:17` | One command for every developer |
| Status values | `text` + `CHECK` constraint, not a Postgres `ENUM` | Easier to change in a migration |

## 5. Schema v1

The columns mirror `models.Lead`, so the adapter can convert rows to `Lead` objects one-to-one. Text columns are `NOT NULL DEFAULT ''`, matching the model's "empty string = unknown". Timestamps become real `timestamptz`, converted to and from ISO strings in the adapter.

```sql
CREATE TABLE leads (
    lead_id           text PRIMARY KEY,
    status            text NOT NULL DEFAULT 'new' CHECK (status IN
                        ('new','qualified','contacted','replied','meeting','won','lost',
                         'bounced','unsubscribed','do_not_contact')),
    company_name      text NOT NULL DEFAULT '',
    website           text NOT NULL DEFAULT '',
    domain            text NOT NULL DEFAULT '',
    email             text NOT NULL DEFAULT '',
    email_status      text NOT NULL DEFAULT '' CHECK (email_status IN ('','valid','risky','invalid','missing')),
    email_type        text NOT NULL DEFAULT '' CHECK (email_type IN ('','personal','role','free_mail')),
    additional_emails text NOT NULL DEFAULT '',
    contact_name      text NOT NULL DEFAULT '',
    contact_title     text NOT NULL DEFAULT '',
    phone             text NOT NULL DEFAULT '',
    address           text NOT NULL DEFAULT '',
    city              text NOT NULL DEFAULT '',
    region            text NOT NULL DEFAULT '',
    country           text NOT NULL DEFAULT '',
    industry          text NOT NULL DEFAULT '',
    linkedin_url      text NOT NULL DEFAULT '',
    twitter_url       text NOT NULL DEFAULT '',
    facebook_url      text NOT NULL DEFAULT '',
    instagram_url     text NOT NULL DEFAULT '',
    source            text NOT NULL DEFAULT '',
    source_url        text NOT NULL DEFAULT '',
    external_id       text NOT NULL DEFAULT '',
    search_keyword    text NOT NULL DEFAULT '',
    search_location   text NOT NULL DEFAULT '',
    lawful_basis      text NOT NULL DEFAULT '',
    tags              text NOT NULL DEFAULT '',
    owner             text NOT NULL DEFAULT '',
    notes             text NOT NULL DEFAULT '',
    last_contacted_at timestamptz,
    next_follow_up_at timestamptz,
    campaign          text NOT NULL DEFAULT '',
    sequence_step     text NOT NULL DEFAULT '',
    run_id            text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Identity rules the pipeline already assumes, now enforced (also across concurrent runs)
CREATE UNIQUE INDEX leads_domain_uq ON leads (domain)       WHERE domain <> '';
CREATE UNIQUE INDEX leads_email_uq  ON leads (lower(email)) WHERE email  <> '';

-- Filters used by the CLI, API, dashboard and future email workers
CREATE INDEX leads_status_idx       ON leads (status);
CREATE INDEX leads_email_status_idx ON leads (email_status);
CREATE INDEX leads_created_idx      ON leads (created_at DESC);
CREATE INDEX leads_follow_up_idx    ON leads (next_follow_up_at) WHERE next_follow_up_at IS NOT NULL;
CREATE INDEX leads_industry_idx     ON leads (lower(industry));
CREATE INDEX leads_place_idx        ON leads (lower(country), lower(city));

CREATE TABLE activities (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lead_id     text NOT NULL REFERENCES leads (lead_id) ON DELETE CASCADE,  -- erasure removes history too
    "timestamp" timestamptz NOT NULL DEFAULT now(),
    type        text NOT NULL,
    details     text NOT NULL DEFAULT '',
    campaign    text NOT NULL DEFAULT ''
);
CREATE INDEX activities_lead_idx ON activities (lead_id, "timestamp");

CREATE TABLE suppression (
    hash     text PRIMARY KEY CHECK (hash ~ '^[0-9a-f]{64}$'),  -- SHA-256, never the address itself
    kind     text NOT NULL CHECK (kind IN ('email','domain')),
    reason   text NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runs (
    run_id      text PRIMARY KEY,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    search      text NOT NULL DEFAULT '',
    sources     text NOT NULL DEFAULT '',
    discovered  integer NOT NULL DEFAULT 0,
    crawled     integer NOT NULL DEFAULT 0,
    new         integer NOT NULL DEFAULT 0,
    duplicates  integer NOT NULL DEFAULT 0,
    merged      integer NOT NULL DEFAULT 0,
    suppressed  integer NOT NULL DEFAULT 0,
    skipped     integer NOT NULL DEFAULT 0,
    errors      integer NOT NULL DEFAULT 0,
    notes       text NOT NULL DEFAULT ''
);
CREATE INDEX runs_started_idx ON runs (started_at DESC);

-- Phase 4 (crawl state, replaces data/state.db)
CREATE TABLE crawled (domain text PRIMARY KEY, last_crawled_at timestamptz NOT NULL, outcome text NOT NULL);
CREATE TABLE geocode (query  text PRIMARY KEY, result jsonb NOT NULL, at timestamptz NOT NULL);
```

**Behaviour differences to handle in the adapter and tests:**

- **Date-only values come back as timestamps.** `next_follow_up_at = "2026-01-02"` is returned as `2026-01-02T00:00:00+00:00`. Either accept this (and update `tests/test_api.py`), or make `next_follow_up_at` a `date` column.
- **Unique indexes reject rows the old stores accepted.** `add_leads` uses `INSERT … ON CONFLICT DO NOTHING` and must return how many rows were actually inserted, so run stats count the rest as duplicates rather than as new.
- **Ordering.** SQL must reproduce the current sort orders explicitly (newest leads first; activities oldest first).

## 6. Phases

Sizes are relative: **S** is small, **M** medium, **L** large. Each phase ships on its own and leaves SQLite and Sheets working.

### Phase 0: Prerequisites (S)

- [x] Fix the two known data-quality bugs **before** copying data, so bad rows aren't migrated:
  - `mailto://` links leave a `//` prefix on emails, which validation then marks `valid` (`extract/emails.py`).
  - Practices that list an NHS page as their website are merged into one lead, because `www.nhs.uk` is treated as a business domain (`normalize.is_non_website`).
- [ ] Decide the open questions in section 10 (at least hosting). *Still open; local PostgreSQL in use until then.*
- [x] Add `docker-compose.yml` for local Postgres:

  ```yaml
  services:
    postgres:
      image: postgres:17
      environment: {POSTGRES_USER: leadgen, POSTGRES_PASSWORD: leadgen, POSTGRES_DB: leadgen}  # local only
      ports: ["5432:5432"]
      volumes: [pgdata:/var/lib/postgresql/data]
  volumes: {pgdata: {}}
  ```

### Phase 1: `PostgresStore` behind the existing interface (M)

- [x] `pyproject.toml`: add the `postgres` extra (section 4).
- [x] `config.py`: `store: Literal["sheets", "sqlite", "postgres"]`; `Secrets.database_url` (read from `DATABASE_URL`).
- [x] `store/__init__.py` `open_store`: add a `postgres` branch, with a clear error if `DATABASE_URL` is missing or the `postgres` extra isn't installed.
- [x] `cli.py`: accept `--store postgres`. `leads doctor` checks the connection, the server version, and whether migrations are up to date.
- [x] `src/leadgen/store/postgres_store.py`: implements all 12 `LeadStore` methods:
  - a connection pool, with each method running in one transaction;
  - `update_leads` only writes whitelisted `LEAD_COLUMNS` (never `lead_id`) and sets `updated_at`, like `SQLiteStore`;
  - string ↔ `timestamptz` conversion in one place.
- [x] `src/leadgen/store/migrations/`: Alembic environment plus `0001_initial` (the section 5 schema, without the Phase 4 tables).
- [x] New commands: `leads db migrate` (upgrade to latest) and `leads db status` (current and latest revision).
- [x] `data/env.example`: `DATABASE_URL=postgresql://leadgen:leadgen@localhost:5432/leadgen`.
- [x] `tests/test_store_contract.py`: the same tests for every backend (SQLite, fake Sheets, Postgres). The Postgres variant runs only when `TEST_DATABASE_URL` is set, and isolates each test in its own schema.

**Done when:** the contract tests pass on all three backends; `leads doctor --store postgres` is all `ok`; and `leads run dental-uk --store postgres --dry-run` works.

**Delivered,** including changes the plan didn't list:

- `add_leads` returns the number of rows inserted, on every backend. The pipeline counts rows the database rejected as duplicates, not as new.
- The Sheets backend now skips an existing `lead_id`, like the other two. Previously it appended a second row.
- `list_suppression()` was added to the protocol, so the importer can copy kind, reason and date as well as the hashes.
- `PostgresStore.update_leads` wraps each lead in a savepoint, so one domain or email conflict doesn't undo the other updates in the same batch.
- The API's global store lock is skipped for PostgreSQL (the connection pool is safe for concurrent use), and lazy store creation is guarded.
- The scheduler closes its store after each job, so a long-running scheduler doesn't leak database connections.
- `leads db migrate --sql` prints the migration SQL for review.
- All 10 pipeline end-to-end tests also run on PostgreSQL. `tests/test_postgres.py` covers constraints, schema checks, connection errors, the import, the CLI and the API.
- Fixed while testing: Rich swallowed `[dry-run]` in table titles, so `leads run --dry-run` never showed that it was a dry run.

### Phase 2: Move the data (S)

- [x] `leads db import --from sqlite [--path data/leads.db]` and `--from sheets`. They copy leads, activities, suppression and runs.
  - Idempotent (`ON CONFLICT DO NOTHING`), so it is safe to re-run.
  - Prints the counts per table, and any rows rejected by the unique indexes.
  - `load_suppression()` returns only hashes, so the importer reads the full suppression rows (kind, reason, date) directly, or a `list_suppression()` method is added.
- [x] Before importing, report duplicate domains and emails in the source (a single `GROUP BY … HAVING count(*) > 1` query), so conflicts are decided rather than silently dropped. *As built: the oldest lead is kept, and every skipped duplicate is listed in the output. Invalid values (status, timestamps) block the whole import before anything is written.*

**Done when:** row counts match; `leads list --format json` returns the same leads from both stores; spot-checks of 10 leads match field by field.

**Rollback:** the SQLite file is never modified, so switching `store:` back to `sqlite` is the rollback. Anything written to Postgres after the switch is **not** in SQLite.

### Phase 3: Let the database do the filtering (M)

Add query methods to `LeadStore`, with **default implementations** built on `load_leads()` plus `views.filter_leads`. SQLite and Sheets then keep working unchanged, and `PostgresStore` overrides them with SQL.

| New method | Replaces | Callers |
|---|---|---|
| `get_lead(ref) -> Lead \| None` (lead_id, email or domain) | `find_lead(load_leads(), ref)` | `api._get_lead`, `cli.status` |
| `query_leads(filters, *, limit, offset) -> (total, list[Lead])` | `filter_leads(load_leads(), …)` | `api.list_leads`, `api.export`, `cli._filtered` |
| `count_by(field, filters)` | Python `Counter` over all leads | `cli list --group-by`, dashboard charts |
| `dedupe_snapshot()` (only `lead_id, email, domain, company_name, city`) | `load_leads()` at run start | `pipeline.Pipeline.run`. The full lead is fetched only when a merge happens |
| `leads_for_revalidation(recheck_all)` | Loop over all leads | `pipeline.revalidate` |
| `forget(kind, value, entry)` / `purge(cutoff)` in **one transaction** | Several separate calls | `compliance.forget`, `compliance.purge` |

- [ ] `api.ApiState.store_lock` applies only to SQLite and Sheets; the Postgres pool is safe for concurrent use.
- [ ] The dashboard uses `query_leads` with paging for the table and `count_by` for the charts.

**Done when:** no request path loads all leads (except full exports), and `GET /leads` stays fast with a seeded 100,000-lead test database. Measure it, and record the result here.

### Phase 4: Crawl state in Postgres (S, optional)

Only needed when runs happen on more than one machine. Add an `open_state(cfg, secrets)` factory, used in `runner.py`, `api.py` and `cli.py`, that returns the existing SQLite `CrawlState` or a Postgres version (the `crawled` and `geocode` tables). Import `data/state.db` with `leads db import`.

### Phase 5: Email automation groundwork (L, design only here)

Build this once the email provider is chosen. Reserved schema:

| Table | Purpose | Key constraints |
|---|---|---|
| `senders` | Sending mailboxes and domains | daily limit, warm-up schedule |
| `campaigns`, `sequence_steps` | What to send, and when (step number, delay, template) | `UNIQUE (campaign_id, step_no)` |
| `enrollments` | One lead in one campaign: current step, `next_send_at`, state (`active`, `paused`, `finished`, `stopped`) | `UNIQUE (lead_id, campaign_id)` |
| `messages` | Each send: provider message ID, status, sent time | `UNIQUE (enrollment_id, step_no)` (never send a step twice), `UNIQUE (provider_message_id)` |
| `email_events` | Provider webhooks: delivered, open, click, reply, bounce, complaint, unsubscribe | `UNIQUE (provider_event_id)` (webhooks are retried) |

Rules the workers must follow:

1. **Claim work atomically.** In one transaction, select due enrollments with `WHERE state = 'active' AND next_send_at <= now() ORDER BY next_send_at LIMIT 50 FOR UPDATE SKIP LOCKED`. Write the `messages` row *before* calling the provider.
2. **Check right before every send:** the suppression hashes and the lead's current status. Never send to `bounced`, `unsubscribed` or `do_not_contact`.
3. **Every event updates the existing model too.** Add an `activities` row; set the lead's status on reply, bounce or unsubscribe; add a suppression hash on unsubscribe or complaint; stop the enrollment. The CLI, dashboard, API and exports then keep working without changes.
4. **Respect per-sender daily caps and the cold-email rules of the recipient's country.**

## 7. Testing

- **Contract tests** (Phase 1) are the safety net: one test suite, every backend, identical results.
- **The existing tests** for the pipeline, API and CLI also run against Postgres via a fixture parameter, when `TEST_DATABASE_URL` is set.
- **CI:** a GitHub Actions job with a `postgres` service container, running the full suite with `TEST_DATABASE_URL`.
- **Migration test:** run `leads db migrate` on an empty database, then downgrade and upgrade again.

## 8. Operations and security

- **Two database roles:** an owner role for migrations (DDL), and an app role for reads and writes only, which is the one used in `DATABASE_URL`.
- **TLS** (`sslmode=require`) for any database that isn't on `localhost`.
- **Backups:** point-in-time recovery on a managed service, or a nightly `pg_dump` with a regular restore test.
- **Personal data:** `leads forget` and `leads purge` behave as before. Your retention policy also has to cover backups.
- **Connections:** keep the pool small (e.g. 5–10); hosted free tiers have low connection limits. Use the provider's pooled connection URL if it offers one.

## 9. Rollout checklist

1. ✅ Phase 0 done; the database is created; `DATABASE_URL` is set in `.env`.
2. ✅ `uv sync --extra postgres`, then `leads db migrate`, then `leads doctor --store postgres`.
3. ✅ Stop the scheduler and any running jobs (none were running).
4. ✅ Back up `data/leads.db` and `data/state.db` (copy the files) → `data/backup/`.
5. ✅ `leads db import --from sqlite`, then check the counts and spot-checks.
6. ✅ Set `store: postgres` in `config/config.yaml`; restart the API and dashboard; start the scheduler again.
7. ⏳ Run a small real search and confirm new leads appear in Postgres.
8. ⏳ Keep the SQLite backup until the first full week runs cleanly.

## 10. Risks and open decisions

| Risk | Mitigation |
|---|---|
| Stores behave subtly differently (dates, ordering, case) | Contract tests; one place for conversions |
| Unique indexes reject existing duplicates | Report duplicates before import; decide case by case |
| Sheets users lose the live sheet | Keep Sheets as an optional export or one-way sync |
| Hosted free-tier connection limits | Small pool; pooled connection URL |
| Cut-over writes lost | Stop the scheduler during import; SQLite file kept as a backup |

**Open decisions:**

1. **Hosting:** local Docker only, a managed service (Supabase, Neon, AWS RDS, …), or your own server?
2. **Google Sheets:** keep it as a one-way export, or drop it once Postgres is live?
3. **Crawl state:** move it now (Phase 4), or only when a second machine runs scrapes?
4. **Email provider** (decides the webhook and event design in Phase 5): Amazon SES, Postmark, SendGrid, Mailgun, Gmail/Google Workspace API, …?
