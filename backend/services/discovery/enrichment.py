"""Stage C + D — enrichment and rescoring.

Runs entirely after qualified leads are persisted and visible. Every lead carries
its own ``enrichment_status``, so a provider outage leaves the lead in place to be
retried instead of removing it from the result set.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.config import ENRICHMENT_PARALLELISM, OPENAI_API_KEY, WORKER_LEASE_SECONDS
from backend.models import (
    EnrichmentStatus,
    LeadRecord,
    QueuedJob,
    QueuedJobStatus,
    SearchJob,
    SearchJobStatus,
)
from backend.observability import log_event
from backend.services import search_state
from backend.services.CompanyModule import lookup_company
from backend.services.CompanyModule.finder import is_usable_company
from backend.services.EmailModule.intelligence import EmailEnrichmentService
from backend.services.EmailModule.store import MemoryEmailStore
from backend.services.EmailModule.types import EmailStatus, PersonInput
from backend.services.PhoneModule import discover_public_phones
from backend.services.provider_health import ProviderCircuit
from backend.services.jobqueue import queue

logger = logging.getLogger(__name__)

BATCH_SIZE = 32

#: How long a lead that hit a provider rate limit waits before being retried.
RATE_LIMIT_BACKOFF_MINUTES = 15

#: Lease held while a lead is being enriched. If the worker dies mid-batch the
#: lease expires and the lead is picked up again, instead of sitting in
#: ``processing`` forever.
PROCESSING_LEASE_MINUTES = 10

#: Enrichment is best-effort; stop retrying a lead that keeps failing.
MAX_ENRICHMENT_ATTEMPTS = 3

_FOUND_STATUSES = {
    EmailStatus.FOUND.value,
    EmailStatus.VERIFIED.value,
    EmailStatus.UNVERIFIED.value,
    EmailStatus.INFERRED.value,
    EmailStatus.VERIFY_UNCERTAIN.value,
}


def _heartbeat(db: Session, job: SearchJob) -> None:
    """Keep the queue lease alive so a long email pass is not stolen mid-batch."""
    running = (
        db.query(QueuedJob)
        .filter(
            QueuedJob.job_type == "search_enrichment",
            QueuedJob.status == QueuedJobStatus.RUNNING.value,
        )
        .all()
    )
    for row in running:
        if (row.payload or {}).get("search_job_id") == job.id:
            queue.heartbeat(db, row.id, lease_seconds=WORKER_LEASE_SECONDS)


def pending_leads(db: Session, search_job_id: int, limit: int = BATCH_SIZE) -> list[LeadRecord]:
    """Leads still owed enrichment, highest relevance first.

    ``PROCESSING`` is included so a lead orphaned by a worker restart is retried
    once its lease lapses.
    """
    now = datetime.utcnow()
    return (
        db.query(LeadRecord)
        .filter(
            LeadRecord.search_job_id == search_job_id,
            LeadRecord.enrichment_status.in_(
                [
                    EnrichmentStatus.PENDING.value,
                    EnrichmentStatus.RATE_LIMITED.value,
                    EnrichmentStatus.PROCESSING.value,
                ]
            ),
            LeadRecord.enrichment_attempts < MAX_ENRICHMENT_ATTEMPTS,
            (LeadRecord.enrichment_locked_until.is_(None))
            | (LeadRecord.enrichment_locked_until <= now),
        )
        .order_by(LeadRecord.relevance_score.desc(), LeadRecord.id)
        .limit(limit)
        .all()
    )


def _enrich_company(lead, ddgs, cache: dict) -> None:
    company = (lead.company_name or "").strip()
    if not company or not is_usable_company(company):
        return
    key = company.lower()
    if key not in cache:
        cache[key] = lookup_company(company, ddgs=ddgs, skip_location=False, skip_domain=False)
    profile = cache[key]
    if not profile:
        return
    if getattr(profile, "location", "") and not lead.company_location:
        lead.company_location = profile.location
    if getattr(profile, "domain", "") and not lead.company_domain:
        lead.company_domain = profile.domain


class _LeadShim:
    """Stand-in for a LeadRecord so company lookup can run off the DB thread."""

    def __init__(self, company: str, location: str, domain: str) -> None:
        self.company_name = company
        self.company_location = location
        self.company_domain = domain


def _network_enrich(snapshot: dict, *, use_providers: bool):
    """Email + phone lookup. No database: safe to run on a worker thread.

    Company SERP lookup is skipped here on purpose: it used the same engine pool
    as the address harvest and doubled the time to the first email.
    """
    shim = _LeadShim(snapshot["company"], snapshot["location"], snapshot["domain"])
    email_result = None
    phone_hit = None
    first_error: Exception | None = None

    if not snapshot.get("email"):
        try:
            service = EmailEnrichmentService(store=MemoryEmailStore())
            email_result = service.enrich(
                PersonInput(
                    name=snapshot["name"],
                    job_title=snapshot["title"],
                    company_name=snapshot["company"],
                    company_domain=shim.company_domain or snapshot["domain"],
                    linkedin_url=snapshot["linkedin"],
                    snippet=snapshot["snippet"],
                    lead_id=snapshot["id"],
                    search_id=snapshot["search_id"],
                ),
                use_providers=use_providers,
                use_public_web=True,
                verify=True,
            )
        except Exception as exc:
            first_error = exc

    if not snapshot.get("phone"):
        try:
            hits = discover_public_phones(
                snapshot["name"],
                company=snapshot["company"],
                snippet=snapshot["snippet"],
            )
            if hits:
                phone_hit = hits[0]
        except Exception as exc:
            first_error = first_error or exc

    if first_error is not None and email_result is None and phone_hit is None:
        return snapshot["id"], None, None, shim, first_error
    return snapshot["id"], email_result, phone_hit, shim, None


def _apply_email(lead: LeadRecord, result) -> bool:
    """Copy an email result onto the lead. Returns True when an address was found.

    A lookup that comes back empty must not wipe an address harvested earlier
    (typically from the SERP snippet at promote time).
    """
    found = bool(result.email) and result.status in _FOUND_STATUSES
    if found:
        lead.email = result.email
        lead.email_status = result.status
        lead.email_confidence = result.confidence
        lead.email_source = result.source or lead.email_source
        lead.email_pattern = result.pattern or None
        lead.email_source_url = result.source_url or None
        lead.email_evidence = [item.to_dict() for item in result.evidence] or None
        lead.email_verification = result.verification.to_dict() if result.verification else None
        lead.email_enriched_at = datetime.utcnow()
        if result.status == EmailStatus.VERIFIED.value:
            lead.email_verified_at = datetime.utcnow()
        if result.domain and not lead.company_domain:
            lead.company_domain = result.domain
        return True
    if lead.email:
        lead.email_enriched_at = datetime.utcnow()
        if not lead.email_status:
            lead.email_status = EmailStatus.FOUND.value
        return True
    lead.email = None
    lead.email_status = result.status
    lead.email_confidence = result.confidence
    lead.email_source = result.source or None
    lead.email_pattern = result.pattern or None
    lead.email_source_url = result.source_url or None
    lead.email_evidence = [item.to_dict() for item in result.evidence] or None
    lead.email_verification = result.verification.to_dict() if result.verification else None
    lead.email_enriched_at = datetime.utcnow()
    if result.domain and not lead.company_domain:
        lead.company_domain = result.domain
    return False


def _sync_counters(db: Session, job: SearchJob) -> None:
    """Derived from the rows, not accumulated.

    A lead reclaimed after a worker restart is attempted twice but must only
    be counted once.
    """
    job.enrichment_attempted = (
        db.query(LeadRecord)
        .filter(
            LeadRecord.search_job_id == job.id,
            LeadRecord.enrichment_attempts > 0,
        )
        .count()
    )
    job.enrichment_completed = (
        db.query(LeadRecord)
        .filter(
            LeadRecord.search_job_id == job.id,
            LeadRecord.enrichment_status.in_(
                [EnrichmentStatus.ENRICHED.value, EnrichmentStatus.NO_MATCH.value]
            ),
        )
        .count()
    )
    job.emails_found = (
        db.query(LeadRecord)
        .filter(LeadRecord.search_job_id == job.id, LeadRecord.email.isnot(None))
        .count()
    )
    job.phones_found = (
        db.query(LeadRecord)
        .filter(LeadRecord.search_job_id == job.id, LeadRecord.phone.isnot(None))
        .count()
    )
    job.message = (
        f"{job.qualified_leads} relevant leads · "
        f"{job.emails_found} emails · {job.phones_found} phones so far"
    )


def _refresh_summary(lead: LeadRecord, job: SearchJob) -> None:
    """Upgrade the placeholder summary to an OpenAI one now that we are off the
    discovery path."""
    if not OPENAI_API_KEY:
        return
    from backend.services.lead_enrichment import to_full_lead

    shaped = to_full_lead(
        {
            "name": lead.name,
            "title": lead.work_designation or "",
            "company": lead.company_name or "",
            "location": lead.personal_location or "",
            "snippet": lead.raw_snippet or "",
            "activity_status": lead.activity_status or "",
            "relevance_score": lead.relevance_score,
        },
        prompt=job.prompt,
        filters=dict(job.filters or {}),
        allow_openai=True,
    )
    if shaped.get("ai_description"):
        lead.ai_description = shaped["ai_description"]


def run_enrichment(db: Session, job: SearchJob, *, use_providers: bool = True) -> None:
    """Enrich the persisted leads of one search."""
    started = time.monotonic()
    previous_status = job.status
    previous_message = job.message

    job.status = SearchJobStatus.ENRICHING
    job.current_stage = "enrichment"
    db.commit()

    circuit = ProviderCircuit()
    processed = 0
    emails = 0
    rate_limited = 0
    workers = max(1, ENRICHMENT_PARALLELISM)

    while True:
        batch = pending_leads(db, job.id)
        if not batch:
            break
        lease_until = datetime.utcnow() + timedelta(minutes=PROCESSING_LEASE_MINUTES)
        for lead in batch:
            lead.enrichment_status = EnrichmentStatus.PROCESSING.value
            lead.enrichment_attempts = int(lead.enrichment_attempts or 0) + 1
            lead.enrichment_locked_until = lease_until
        db.commit()

        done_already = [lead for lead in batch if lead.email and lead.phone]
        need_net = [lead for lead in batch if not (lead.email and lead.phone)]
        for lead in done_already:
            lead.enrichment_status = EnrichmentStatus.ENRICHED.value
            lead.enrichment_error = None
            lead.enrichment_locked_until = None
            if not lead.email_status:
                lead.email_status = EmailStatus.FOUND.value
            emails += 1
            processed += 1
        if done_already:
            db.commit()
            _sync_counters(db, job)
            db.commit()
            _heartbeat(db, job)
        if not need_net:
            continue

        snapshots = [
            {
                "id": lead.id,
                "search_id": job.id,
                "name": lead.name or "",
                "title": lead.work_designation or "",
                "company": lead.company_name or "",
                "location": lead.company_location or "",
                "domain": lead.company_domain or "",
                "linkedin": lead.linkedin_url or "",
                "snippet": lead.raw_snippet or "",
                "email": lead.email or "",
                "phone": lead.phone or "",
            }
            for lead in need_net
        ]
        by_id = {lead.id: lead for lead in need_net}
        allow_providers = use_providers and circuit.any_available()

        with ThreadPoolExecutor(
            max_workers=min(workers, len(snapshots)),
            thread_name_prefix="mark-enrich",
        ) as pool:
            pending = {
                pool.submit(_network_enrich, snap, use_providers=allow_providers)
                for snap in snapshots
            }
            while pending:
                done, pending = wait(pending, timeout=10, return_when=FIRST_COMPLETED)
                for fut in done:
                    lead_id, result, phone_hit, shim, error = fut.result()
                    lead = by_id[lead_id]
                    try:
                        if error is not None and result is None and phone_hit is None:
                            raise error
                        if shim.company_location and not lead.company_location:
                            lead.company_location = shim.company_location
                        if shim.company_domain and not lead.company_domain:
                            lead.company_domain = shim.company_domain
                        found_email = False
                        if result is not None:
                            found_email = _apply_email(lead, result)
                        elif lead.email:
                            found_email = True
                        found_phone = False
                        if phone_hit and not lead.phone:
                            lead.phone = phone_hit["phone"]
                            lead.phone_source = "public_web" if phone_hit.get("url") else "snippet"
                            found_phone = True
                        elif lead.phone:
                            found_phone = True
                        _refresh_summary(lead, job)
                        if found_email or found_phone:
                            lead.enrichment_status = EnrichmentStatus.ENRICHED.value
                            if found_email:
                                emails += 1
                        else:
                            lead.enrichment_status = EnrichmentStatus.NO_MATCH.value
                        lead.enrichment_error = None
                        lead.enrichment_locked_until = None
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        db.refresh(lead)
                        kind = type(exc).__name__
                        if circuit.looks_rate_limited(exc):
                            lead.enrichment_status = EnrichmentStatus.RATE_LIMITED.value
                            lead.enrichment_locked_until = datetime.utcnow() + timedelta(
                                minutes=RATE_LIMIT_BACKOFF_MINUTES
                            )
                            rate_limited += 1
                        else:
                            lead.enrichment_status = EnrichmentStatus.FAILED.value
                        lead.enrichment_error = kind[:255]
                        db.commit()
                        search_state.bump(db, job, errors_count=1)
                        log_event(
                            logger,
                            "enrichment.lead_failed",
                            level=logging.WARNING,
                            trace_id=job.trace_id,
                            search_id=job.id,
                            lead_id=lead.id,
                            error=kind,
                        )
                    processed += 1
                    _sync_counters(db, job)
                    db.commit()
                    _heartbeat(db, job)
                if not done:
                    _heartbeat(db, job)

    # Enrichment never changes the discovery verdict; restore it.
    job.status = (
        previous_status
        if previous_status in (SearchJobStatus.COMPLETED, SearchJobStatus.PARTIALLY_COMPLETED)
        else SearchJobStatus.COMPLETED
    )
    job.current_stage = None
    job.progress = 100
    job.completed_at = job.completed_at or datetime.utcnow()
    parts = [f"{job.qualified_leads} relevant leads"]
    if job.emails_found:
        parts.append(f"{job.emails_found} emails")
    if job.phones_found:
        parts.append(f"{job.phones_found} phones")
    job.message = " · ".join(parts) if len(parts) > 1 else (
        previous_message or parts[0]
    )
    db.commit()

    log_event(
        logger,
        "enrichment.complete",
        trace_id=job.trace_id,
        search_id=job.id,
        processed=processed,
        emails_found=emails,
        phones_found=int(job.phones_found or 0),
        rate_limited=rate_limited,
        providers_open=",".join(circuit.open_providers()) or None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
