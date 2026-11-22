"""Initial schema: leads, activities, suppression, runs.

Columns mirror leadgen.models.Lead (text, '' = unknown) with real timestamps, CHECK constraints for the
status vocabularies, and unique indexes for the identities the pipeline deduplicates on (domain, email),
so even two concurrent runs can't store the same business twice.

Revision ID: 0001
Revises:
Create Date: 2026-11-22
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TEXT_COLUMNS = [
    "company_name", "website", "domain", "email", "additional_emails", "contact_name", "contact_title",
    "phone", "address", "city", "region", "country", "industry", "linkedin_url", "twitter_url",
    "facebook_url", "instagram_url", "source", "source_url", "external_id", "search_keyword",
    "search_location", "lawful_basis", "tags", "owner", "notes", "campaign", "sequence_step", "run_id",
]  # fmt: skip
STATUSES = ("new", "qualified", "contacted", "replied", "meeting", "won", "lost",
            "bounced", "unsubscribed", "do_not_contact")  # fmt: skip


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    text_cols = ",\n    ".join(f"{c} text NOT NULL DEFAULT ''" for c in TEXT_COLUMNS)
    op.execute(f"""
CREATE TABLE leads (
    lead_id text PRIMARY KEY,
    status text NOT NULL DEFAULT 'new' CHECK (status IN ({_in(STATUSES)})),
    email_status text NOT NULL DEFAULT '' CHECK (email_status IN ('', 'valid', 'risky', 'invalid', 'missing')),
    email_type text NOT NULL DEFAULT '' CHECK (email_type IN ('', 'personal', 'role', 'free_mail')),
    {text_cols},
    last_contacted_at timestamptz,
    next_follow_up_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)""")
    for stmt in (
        "CREATE UNIQUE INDEX leads_domain_uq ON leads (domain) WHERE domain <> ''",
        "CREATE UNIQUE INDEX leads_email_uq ON leads (lower(email)) WHERE email <> ''",
        "CREATE INDEX leads_status_idx ON leads (status)",
        "CREATE INDEX leads_email_status_idx ON leads (email_status)",
        "CREATE INDEX leads_created_idx ON leads (created_at DESC)",
        "CREATE INDEX leads_follow_up_idx ON leads (next_follow_up_at) WHERE next_follow_up_at IS NOT NULL",
        "CREATE INDEX leads_industry_idx ON leads (lower(industry))",
        "CREATE INDEX leads_place_idx ON leads (lower(country), lower(city))",
        """CREATE TABLE activities (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            lead_id text NOT NULL REFERENCES leads (lead_id) ON DELETE CASCADE,
            "timestamp" timestamptz NOT NULL DEFAULT now(),
            type text NOT NULL,
            details text NOT NULL DEFAULT '',
            campaign text NOT NULL DEFAULT ''
        )""",
        'CREATE INDEX activities_lead_idx ON activities (lead_id, "timestamp")',
        """CREATE TABLE suppression (
            hash text PRIMARY KEY CHECK (hash ~ '^[0-9a-f]{64}$'),
            kind text NOT NULL CHECK (kind IN ('email', 'domain')),
            reason text NOT NULL DEFAULT '',
            added_at timestamptz NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE runs (
            run_id text PRIMARY KEY,
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            search text NOT NULL DEFAULT '',
            sources text NOT NULL DEFAULT '',
            discovered integer NOT NULL DEFAULT 0,
            crawled integer NOT NULL DEFAULT 0,
            new integer NOT NULL DEFAULT 0,
            duplicates integer NOT NULL DEFAULT 0,
            merged integer NOT NULL DEFAULT 0,
            suppressed integer NOT NULL DEFAULT 0,
            skipped integer NOT NULL DEFAULT 0,
            errors integer NOT NULL DEFAULT 0,
            notes text NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX runs_started_idx ON runs (started_at DESC)",
    ):
        op.execute(stmt)


def downgrade() -> None:
    for table in ("activities", "runs", "suppression", "leads"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
