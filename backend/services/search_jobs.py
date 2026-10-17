"""Queue handlers for search discovery and enrichment.

Discovery and enrichment are deliberately separate queue entries: a provider
outage or 429 storm during enrichment must not stop or roll back the candidates
discovery already persisted.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.models import JobType, SearchJob
from backend.observability import log_event, new_trace_id
from backend.services.jobqueue import JobHandle, queue
from backend.worker import register

logger = logging.getLogger(__name__)


def enqueue_discovery(db: Session, job: SearchJob) -> None:
    """Queue the discovery stage for a freshly created SearchJob."""
    if not job.trace_id:
        job.trace_id = new_trace_id()
        db.commit()
    queue.enqueue(
        db,
        JobType.SEARCH_DISCOVERY.value,
        {"search_job_id": job.id},
        trace_id=job.trace_id,
    )
    log_event(logger, "discovery.enqueued", trace_id=job.trace_id, search_id=job.id)


def enqueue_enrichment(db: Session, job: SearchJob, *, use_providers: bool = True) -> None:
    """Queue enrichment for a search whose qualified leads are already persisted."""
    queue.enqueue(
        db,
        JobType.SEARCH_ENRICHMENT.value,
        {"search_job_id": job.id, "use_providers": use_providers},
        trace_id=job.trace_id,
    )
    log_event(logger, "enrichment.enqueued", trace_id=job.trace_id, search_id=job.id)


@register(JobType.SEARCH_DISCOVERY.value)
def handle_discovery(db: Session, handle: JobHandle) -> None:
    from backend.services.discovery.runner import run_discovery

    search_job_id = int(handle.payload.get("search_job_id") or 0)
    job = db.get(SearchJob, search_job_id)
    if job is None:
        log_event(
            logger,
            "discovery.missing_job",
            level=logging.WARNING,
            job_id=handle.id,
            search_id=search_job_id,
        )
        return
    run_discovery(db, job, final_attempt=handle.is_final_attempt)


@register(JobType.SEARCH_ENRICHMENT.value)
def handle_enrichment(db: Session, handle: JobHandle) -> None:
    from backend.services.discovery.enrichment import run_enrichment

    search_job_id = int(handle.payload.get("search_job_id") or 0)
    job = db.get(SearchJob, search_job_id)
    if job is None:
        log_event(
            logger,
            "enrichment.missing_job",
            level=logging.WARNING,
            job_id=handle.id,
            search_id=search_job_id,
        )
        return
    run_enrichment(db, job, use_providers=bool(handle.payload.get("use_providers", True)))
