"""Apply results to leads and run bulk Find Emails in a background thread."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from backend.config import DATABASE_URL
from backend.database import SessionLocal
from backend.models import EmailEnrichmentJob, LeadRecord
from backend.services.EmailModule.intelligence import EmailEnrichmentService
from backend.services.EmailModule.sql_store import SqlEmailStore
from backend.services.EmailModule.types import EmailResult, EmailStatus, PersonInput

BATCH_SIZE = 25


def apply_result(lead: LeadRecord, result: EmailResult) -> None:
    lead.email = result.email if result.status != EmailStatus.NOT_FOUND.value else None
    lead.email_status = result.status
    lead.email_confidence = result.confidence
    lead.email_source = result.source or None
    lead.email_pattern = result.pattern or None
    lead.email_source_url = result.source_url or None
    lead.email_evidence = [item.to_dict() for item in result.evidence] or None
    lead.email_verification = result.verification.to_dict() if result.verification else None
    lead.email_verified_at = (
        datetime.utcnow() if result.status == EmailStatus.VERIFIED.value else lead.email_verified_at
    )
    lead.email_enriched_at = datetime.utcnow()
    if result.domain:
        lead.company_domain = result.domain


def person_from_lead(lead: LeadRecord) -> PersonInput:
    return PersonInput(
        name=lead.name or "",
        job_title=lead.work_designation or "",
        company_name=lead.company_name or "",
        company_domain=lead.company_domain or "",
        linkedin_url=lead.linkedin_url or "",
        snippet=lead.raw_snippet or "",
        lead_id=lead.id,
        search_id=lead.search_job_id,
    )


def enrich_lead_record(
    db: Session,
    lead: LeadRecord,
    *,
    use_providers: bool = False,
    use_public_web: bool = True,
    verify: bool = True,
    force: bool = False,
    service: EmailEnrichmentService | None = None,
) -> EmailResult:
    svc = service or EmailEnrichmentService(store=SqlEmailStore(db))
    result = svc.enrich(
        person_from_lead(lead),
        use_providers=use_providers,
        use_public_web=use_public_web,
        verify=verify,
        force=force,
    )
    apply_result(lead, result)
    db.commit()
    db.refresh(lead)
    return result


def chunked(items: list[int], size: int) -> Iterable[list[int]]:
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]


def enrich_lead_ids(
    db: Session,
    lead_ids: list[int],
    *,
    use_providers: bool = True,
    use_public_web: bool = True,
    batch_size: int = BATCH_SIZE,
    on_progress: Callable[[int, int, EmailResult | None], None] | None = None,
    service: EmailEnrichmentService | None = None,
) -> dict[str, int]:
    """Process ids in batches. Sequential on SQLite to avoid write locks."""
    stats = {"processed": 0, "found": 0, "inferred": 0, "verified": 0, "not_found": 0}
    svc = service or EmailEnrichmentService(store=SqlEmailStore(db))
    total = len(lead_ids)
    for group in chunked(lead_ids, batch_size):
        for lead_id in group:
            lead = db.get(LeadRecord, lead_id)
            if not lead:
                continue
            result = enrich_lead_record(
                db,
                lead,
                use_providers=use_providers,
                use_public_web=use_public_web,
                service=svc,
            )
            stats["processed"] += 1
            if result.status == EmailStatus.VERIFIED.value:
                stats["verified"] += 1
            elif result.status in {EmailStatus.FOUND.value, EmailStatus.UNVERIFIED.value}:
                stats["found"] += 1
            elif result.status in {EmailStatus.INFERRED.value, EmailStatus.VERIFY_UNCERTAIN.value}:
                stats["inferred"] += 1
            else:
                stats["not_found"] += 1
            if on_progress:
                on_progress(stats["processed"], total, result)
    return stats


def latest_email_job(db: Session, search_id: int) -> EmailEnrichmentJob | None:
    return (
        db.query(EmailEnrichmentJob)
        .filter(EmailEnrichmentJob.search_job_id == search_id)
        .order_by(EmailEnrichmentJob.id.desc())
        .first()
    )


def start_email_job(db: Session, search_id: int, total: int) -> EmailEnrichmentJob:
    job = EmailEnrichmentJob(
        search_job_id=search_id,
        status="running",
        progress=1,
        message="Finding emails…",
        total=total,
        created_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def execute_email_job(job_id: int, lead_ids: list[int], *, use_providers: bool = True) -> None:
    db = SessionLocal()
    try:
        job = db.get(EmailEnrichmentJob, job_id)
        if not job:
            return

        def on_progress(done: int, total: int, result: EmailResult | None) -> None:
            job.processed = done
            job.total = total
            job.progress = min(99, int(100 * done / max(total, 1)))
            if result:
                if result.status == EmailStatus.VERIFIED.value:
                    job.verified += 1
                elif result.status in {EmailStatus.FOUND.value, EmailStatus.UNVERIFIED.value}:
                    job.found += 1
                elif result.status in {EmailStatus.INFERRED.value, EmailStatus.VERIFY_UNCERTAIN.value}:
                    job.inferred += 1
                else:
                    job.not_found += 1
            job.message = f"Checked {done}/{total} people"
            db.commit()

        enrich_lead_ids(
            db,
            lead_ids,
            use_providers=use_providers,
            on_progress=on_progress,
            batch_size=10 if DATABASE_URL.startswith("sqlite") else BATCH_SIZE,
        )
        job.status = "completed"
        job.progress = 100
        job.finished_at = datetime.utcnow()
        job.message = (
            f"Done · {job.verified} verified · {job.found} found · "
            f"{job.inferred} inferred · {job.not_found} not found"
        )
        db.commit()
    except Exception as exc:
        job = db.get(EmailEnrichmentJob, job_id)
        if job:
            job.status = "failed"
            job.message = str(exc)
            job.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def spawn_email_job(job_id: int, lead_ids: list[int], *, use_providers: bool = True) -> None:
    threading.Thread(
        target=execute_email_job,
        args=(job_id, lead_ids),
        kwargs={"use_providers": use_providers},
        daemon=True,
    ).start()
