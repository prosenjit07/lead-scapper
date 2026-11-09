"""Lead pipeline: discover -> early dedupe -> crawl website -> extract -> validate -> policy ->
enrich -> final dedupe -> batched write. Errors are isolated per candidate and per source."""

from __future__ import annotations

import asyncio
import csv
import re
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path

from leadgen.compliance import SuppressionList
from leadgen.config import AppConfig
from leadgen.dedupe import DedupeIndex, dedupe_keys, lead_keys, make_lead_id
from leadgen.enrich.base import Enricher
from leadgen.extract.emails import clean_email
from leadgen.extract.industry import IndustryMap
from leadgen.extract.jsonld import Person
from leadgen.extract.social import classify_social
from leadgen.extract.website import WebsiteCrawler, WebsiteData
from leadgen.http.fetcher import PoliteFetcher
from leadgen.logging_setup import get_logger
from leadgen.models import (
    LEAD_COLUMNS,
    MERGEABLE_FIELDS,
    Candidate,
    EmailStatus,
    EmailType,
    Lead,
    LeadStatus,
    RunStats,
    SearchQuery,
    utcnow_iso,
)
from leadgen.normalize import (
    clean_text,
    country_name,
    is_non_website,
    normalize_phone,
    normalize_place,
    normalize_url,
    registered_domain,
    site_root,
    split_location,
)
from leadgen.sources.base import Source, SourceError
from leadgen.store.base import LeadStore
from leadgen.store.state import CrawlState
from leadgen.validate.email import EmailCheck, EmailValidator, rank

log = get_logger("pipeline")

WRITE_BATCH = 50
DECISION_MAKER_RE = re.compile(
    r"owner|founder|director|ceo|chief|principal|managing|manager|partner|head|president|proprietor|"
    r"inhaber|geschäftsführer|gerente|directeur",
    re.I,
)
SOCIAL_FIELDS = {"facebook": "facebook_url", "instagram": "instagram_url", "twitter": "twitter_url",
                 "linkedin_company": "linkedin_url"}  # fmt: skip
CANDIDATE_MERGE_FIELDS = ["company_name", "website", "domain", "phone", "address", "city", "region", "country",
                          "industry", "facebook_url", "instagram_url", "twitter_url", "linkedin_url"]  # fmt: skip


@dataclass
class RunResult:
    stats: RunStats
    leads: list[Lead] = field(default_factory=list)  # new leads written (or that would be, in dry-run)


class BatchWriter:
    """Buffers new leads and field merges; writes in batches. On store failure, rows are saved to a
    local CSV so nothing collected is lost."""

    def __init__(self, store: LeadStore, stats: RunStats, dry_run: bool, fallback_dir: Path,
                 batch_size: int = WRITE_BATCH) -> None:  # fmt: skip
        self.store = store
        self.stats = stats
        self.dry_run = dry_run
        self.fallback_dir = fallback_dir
        self.batch_size = batch_size
        self.pending: dict[str, Lead] = {}
        self.updates: dict[str, dict[str, str]] = {}
        self.written: list[Lead] = []
        self._lock = asyncio.Lock()

    async def add(self, lead: Lead) -> None:
        self.pending[lead.lead_id] = lead
        if len(self.pending) >= self.batch_size:
            await self._flush_new()

    def merge(self, lead_id: str, fields: dict[str, str]) -> None:
        if lead_id in self.pending:
            for k, v in fields.items():
                setattr(self.pending[lead_id], k, v)
        else:
            self.updates.setdefault(lead_id, {}).update(fields)

    async def _flush_new(self) -> None:
        async with self._lock:
            if not self.pending:
                return
            batch = list(self.pending.values())
            self.pending = {}
            self.written.extend(batch)
            if self.dry_run:
                return
            try:
                await asyncio.to_thread(self.store.add_leads, batch)
            except Exception as e:
                self.stats.errors += 1
                path = self._save_fallback(batch)
                self.stats.add_note(f"store write failed ({e}); {len(batch)} leads saved to {path}")
                log.error(
                    "store write failed, saved batch locally", extra={"path": str(path), "error": str(e)}
                )

    async def flush(self) -> None:
        await self._flush_new()
        if self.updates and not self.dry_run:
            updates, self.updates = self.updates, {}
            try:
                await asyncio.to_thread(self.store.update_leads, updates)
            except Exception as e:
                self.stats.errors += 1
                self.stats.add_note(f"merge updates failed: {e}")
                log.error("store update failed", extra={"error": str(e)})

    def _save_fallback(self, leads: list[Lead]) -> Path:
        self.fallback_dir.mkdir(parents=True, exist_ok=True)
        path = self.fallback_dir / f"unsaved_{self.stats.run_id}.csv"
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEAD_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerows(lead.to_row() for lead in leads)
        return path


def _match_person(people: list[Person], email: str, etype: EmailType) -> Person | None:
    for p in people:
        if p.email and p.email == email:
            return p
    if etype == EmailType.PERSONAL:
        local = email.split("@")[0]
        for p in people:
            tokens = [t for t in normalize_place(p.name).split() if len(t) >= 3]
            if tokens and (tokens[0] in local or tokens[-1] in local):
                return p
    return None


def _pick_contact(people: list[Person], primary: EmailCheck | None) -> Person | None:
    if not people:
        return None
    if primary and (p := _match_person(people, primary.email, primary.type)):
        return p
    for p in people:
        if DECISION_MAKER_RE.search(p.job_title):
            return p
    return people[0] if len(people) == 1 else None


class Pipeline:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        store: LeadStore,
        state: CrawlState,
        fetcher: PoliteFetcher,
        validator: EmailValidator,
        industries: IndustryMap,
        enrichers: Sequence[Enricher] = (),
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.state = state
        self.validator = validator
        self.industries = industries
        self.enrichers = list(enrichers)
        self.dry_run = dry_run
        self.crawler = WebsiteCrawler(fetcher, cfg.crawl.max_pages_per_site)
        self.index = DedupeIndex()
        self.leads_by_id: dict[str, Lead] = {}
        self.suppression = SuppressionList()
        self.force_recrawl = False

    # ------------------------------------------------------------------ orchestration
    async def run(
        self,
        queries: list[SearchQuery],
        sources: list[Source],
        label: str,
        limit: int | None = None,
        force_recrawl: bool = False,
    ) -> RunResult:
        stats = RunStats(search=label, sources=",".join(s.name for s in sources))
        self.force_recrawl = force_recrawl
        log.info(
            f"run {stats.run_id} started: {label} via {stats.sources}"
            + (" [dry-run]" if self.dry_run else "")
        )

        existing = await asyncio.to_thread(self.store.load_leads)
        self.index = DedupeIndex.from_leads(existing)
        self.leads_by_id = {lead.lead_id: lead for lead in existing}
        self.suppression = SuppressionList(await asyncio.to_thread(self.store.load_suppression))
        writer = BatchWriter(self.store, stats, self.dry_run, Path(self.cfg.sqlite_path).parent)

        slots = asyncio.Semaphore(self.cfg.crawl.concurrency * 2)
        tasks: set[asyncio.Task[None]] = set()
        accepted = 0
        try:
            for query in queries:
                for source in sources:
                    if limit and accepted >= limit:
                        break
                    try:
                        async with aclosing(source.discover(query)) as candidates:  # type: ignore[type-var]
                            async for cand in candidates:
                                stats.discovered += 1
                                decision = self._precheck(cand, query, stats, writer)
                                if decision is None:
                                    continue
                                accepted += 1
                                await slots.acquire()
                                task = asyncio.create_task(
                                    self._process(cand, query, decision, stats, writer)
                                )
                                tasks.add(task)
                                task.add_done_callback(lambda t: (slots.release(), tasks.discard(t)))
                                if limit and accepted >= limit:
                                    break
                    except SourceError as e:
                        stats.errors += 1
                        stats.add_note(f"{source.name} '{query.keyword}' @ '{query.location}': {e}")
                        log.error(f"{source.name} failed for '{query.keyword}' @ '{query.location}': {e}")
                    except Exception as e:
                        stats.errors += 1
                        stats.add_note(
                            f"{source.name} '{query.keyword}' @ '{query.location}': {type(e).__name__}: {e}"
                        )
                        log.exception(f"{source.name} crashed for '{query.keyword}' @ '{query.location}'")
            if tasks:
                await asyncio.gather(*list(tasks))
        finally:
            await writer.flush()
            stats.finished_at = utcnow_iso()
            if not self.dry_run:
                try:
                    await asyncio.to_thread(self.store.log_run, stats)
                except Exception as e:
                    log.error(f"could not record run in store: {e}")
        log.info(
            f"run {stats.run_id} finished: discovered={stats.discovered} crawled={stats.crawled} new={stats.new} "
            f"duplicates={stats.duplicates} merged={stats.merged} suppressed={stats.suppressed} "
            f"skipped={stats.skipped} errors={stats.errors}"
        )
        return RunResult(stats=stats, leads=writer.written)

    # ------------------------------------------------------------------ stage 1: cheap checks
    def _precheck(
        self, cand: Candidate, query: SearchQuery, stats: RunStats, writer: BatchWriter
    ) -> tuple[str, list[str]] | None:
        website = normalize_url(cand.website)
        if website and is_non_website(website):
            website = None
        domain = registered_domain(website) if website else None
        if self.suppression.domain_suppressed(domain):
            stats.suppressed += 1
            return None
        email = clean_email(cand.email) if cand.email and not cand.restricted else None
        if email and self.suppression.email_suppressed(email):
            email = None
            if not website:
                stats.suppressed += 1
                return None
        keys = dedupe_keys(email=email, domain=domain, company_name=cand.company_name, city=cand.city)
        if not keys:
            stats.skipped += 1
            return None

        if existing_id := self.index.find(keys):
            stats.duplicates += 1
            if self.cfg.merge_duplicates and existing_id in self.leads_by_id:
                self._merge_into(existing_id, self._lead_from_candidate(cand, query, existing_id),
                                 CANDIDATE_MERGE_FIELDS, stats, writer)  # fmt: skip
            return None

        if (
            domain
            and not self.force_recrawl
            and self.state.recently_crawled(domain, self.cfg.crawl.recrawl_after_days)
        ):
            stats.skipped += 1
            log.debug("domain crawled recently, skipping", extra={"domain": domain})
            return None

        lead_id = make_lead_id(keys)
        self.index.add(keys, lead_id)  # reserve so the same business from another source is skipped
        return lead_id, keys

    def _merge_into(
        self, lead_id: str, new: Lead, fields: list[str], stats: RunStats, writer: BatchWriter
    ) -> None:
        current = self.leads_by_id.get(lead_id)
        if current is None:
            return
        changes = {f: getattr(new, f) for f in fields if not getattr(current, f) and getattr(new, f)}
        if not changes:
            return
        for k, v in changes.items():
            setattr(current, k, v)
        changes["updated_at"] = utcnow_iso()
        writer.merge(lead_id, changes)
        self.index.add(lead_keys(current), lead_id)
        stats.merged += 1

    # ------------------------------------------------------------------ stage 2: full processing
    async def _process(
        self,
        cand: Candidate,
        query: SearchQuery,
        decision: tuple[str, list[str]],
        stats: RunStats,
        writer: BatchWriter,
    ) -> None:
        lead_id, _ = decision
        try:
            lead = self._lead_from_candidate(cand, query, lead_id)
            lead.run_id = stats.run_id
            site: WebsiteData | None = None

            if lead.website:
                site = await self.crawler.crawl(lead.website)
                stats.crawled += 1
                if lead.domain and not self.dry_run:  # a dry run must not change what later runs skip
                    self.state.mark_crawled(lead.domain, "ok" if site.ok else "failed")
                if site.ok:
                    self._apply_site(lead, site)
                else:
                    log.debug("website not usable", extra={"url": lead.website, "reason": site.error})

            if cand.restricted and not (site and site.ok):
                # Discovery-only source and nothing from the business's own site to store.
                stats.skipped += 1
                return

            await self._apply_emails(lead, cand, site)

            if not lead.email and self.cfg.compliance.require_email:
                stats.skipped += 1
                return
            if not (lead.email or lead.website or lead.phone):
                stats.skipped += 1
                return

            self._finalize(lead, cand, query, site)

            for enricher in self.enrichers:
                lead = await enricher.enrich(lead)

            final_keys = lead_keys(lead)
            if other := self.index.find(final_keys, ignore=lead_id):
                stats.duplicates += 1
                if self.cfg.merge_duplicates:
                    self._merge_into(other, lead, MERGEABLE_FIELDS, stats, writer)
                return
            self.index.add(final_keys, lead_id)
            self.leads_by_id[lead_id] = lead
            await writer.add(lead)
            stats.new += 1
            log.info(f"+ {lead.company_name or lead.domain} | {lead.email or '-'} ({lead.email_status}) | "
                     f"{lead.city or '-'}, {lead.country or '-'}")  # fmt: skip
        except Exception as e:
            stats.errors += 1
            stats.add_note(f"{cand.website or cand.company_name}: {type(e).__name__}: {e}")
            log.warning(f"failed to process {cand.website or cand.company_name}: {e}", exc_info=True)

    def _lead_from_candidate(self, cand: Candidate, query: SearchQuery, lead_id: str) -> Lead:
        now = utcnow_iso()
        website = normalize_url(cand.website)
        lead = Lead(
            lead_id=lead_id,
            status=LeadStatus.NEW.value,
            source=cand.source,
            source_url=cand.source_url or "",
            external_id=cand.external_id or "",
            search_keyword=query.keyword,
            search_location=query.location,
            lawful_basis=self.cfg.compliance.lawful_basis,
            tags=",".join(query.tags),
            created_at=now,
            updated_at=now,
        )
        if website and is_non_website(website):
            hit = classify_social(website)
            if hit and hit[0] in SOCIAL_FIELDS:
                setattr(lead, SOCIAL_FIELDS[hit[0]], hit[1])
        elif website:
            lead.website = site_root(website)
            lead.domain = registered_domain(website) or ""

        if not cand.restricted:
            lead.company_name = clean_text(cand.company_name, 200)
            lead.country = country_name(cand.country)
            lead.phone = normalize_phone(cand.phone, lead.country or split_location(query.location)[1])
            lead.address = clean_text(cand.address, 300)
            lead.city = clean_text(cand.city, 100)
            lead.region = clean_text(cand.region, 100)
            lead.industry = self.industries.classify(
                cand.categories, query.keyword, query.industry or cand.industry
            )
        return lead

    def _apply_site(self, lead: Lead, site: WebsiteData) -> None:
        ld = site.jsonld
        if site.final_url:
            final_domain = registered_domain(site.final_url)
            if final_domain:
                lead.website, lead.domain = site.final_url, final_domain
        lead.company_name = lead.company_name or ld.org_name or site.site_name
        lead.country = lead.country or country_name(ld.country)
        lead.city = lead.city or ld.city
        lead.region = lead.region or ld.region
        if not lead.address and ld.street:
            lead.address = ", ".join(filter(None, [ld.street, ld.postcode, ld.city]))
        if not lead.phone:
            raw = (ld.telephones or site.phones or [""])[0]
            lead.phone = normalize_phone(raw, lead.country)

        allow_personal = self.cfg.compliance.personal_data == "allow"
        lead.linkedin_url = (
            lead.linkedin_url
            or site.socials.get("linkedin_company", "")
            or (site.socials.get("linkedin_person", "") if allow_personal else "")
        )
        lead.twitter_url = lead.twitter_url or site.socials.get("twitter", "")
        lead.facebook_url = lead.facebook_url or site.socials.get("facebook", "")
        lead.instagram_url = lead.instagram_url or site.socials.get("instagram", "")

    async def _apply_emails(self, lead: Lead, cand: Candidate, site: WebsiteData | None) -> None:
        raw: list[str] = []
        if cand.email and not cand.restricted:
            raw.append(cand.email)
        if site:
            raw.extend(site.emails)
        emails = [e for e in dict.fromkeys(filter(None, (clean_email(x) for x in raw)))]
        emails = [e for e in emails if not self.suppression.email_suppressed(e)]

        checks = await self.validator.check_many(emails, lead.domain or None)
        allow_personal = self.cfg.compliance.personal_data == "allow"
        if not allow_personal:
            checks = [c for c in checks if c.type == EmailType.ROLE]
        ranked = rank(checks, prefer_personal=allow_personal)

        primary = ranked[0] if ranked else None
        if primary:
            lead.email = primary.email
            lead.email_status = primary.status.value
            lead.email_type = primary.type.value
            lead.additional_emails = ",".join(c.email for c in ranked[1:6])
            if site and primary.email in site.email_pages:
                lead.source_url = site.email_pages[primary.email]
        else:
            lead.email_status = EmailStatus.MISSING.value

        if allow_personal and site and (person := _pick_contact(site.jsonld.people, primary)):
            lead.contact_name = person.name
            lead.contact_title = person.job_title

    def _finalize(self, lead: Lead, cand: Candidate, query: SearchQuery, site: WebsiteData | None) -> None:
        q_city, q_country = split_location(query.location)
        lead.city = lead.city or q_city
        lead.country = lead.country or q_country
        if not lead.company_name:
            lead.company_name = lead.domain or ""
        if not lead.source_url:
            lead.source_url = lead.website
        if not lead.industry:
            categories = [] if cand.restricted else cand.categories
            schema_types = site.jsonld.org_types if site else []
            lead.industry = self.industries.classify(
                categories, query.keyword, query.industry or cand.industry, schema_types
            )


async def revalidate(store: LeadStore, validator: EmailValidator, recheck_all: bool = False) -> int:
    """Re-check stored emails (e.g. weekly, before a campaign). Returns number of leads updated."""
    leads = await asyncio.to_thread(store.load_leads)
    updates: dict[str, dict[str, str]] = {}
    for lead in leads:
        if not lead.email:
            continue
        if not recheck_all and lead.email_status in (EmailStatus.VALID.value, EmailStatus.INVALID.value):
            continue
        check = await validator.check(lead.email, lead.domain or None)
        if check.status.value != lead.email_status or check.type.value != lead.email_type:
            updates[lead.lead_id] = {"email_status": check.status.value, "email_type": check.type.value}
    if updates:
        await asyncio.to_thread(store.update_leads, updates)
    return len(updates)
