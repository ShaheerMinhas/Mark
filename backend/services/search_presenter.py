from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models import (
    LEGACY_SEARCH_STATUSES,
    EmailEnrichmentJob,
    JobType,
    LeadRecord,
    QueuedJob,
    SearchJob,
    SearchJobStatus,
    TERMINAL_SEARCH_STATUSES,
)
from backend.schemas import SearchJobOut, SearchProgressOut, SearchRunOut
from backend.services import search_state
from backend.services.search_run import ManualEmailJob, build_search_run


def legacy_status(status: SearchJobStatus) -> SearchJobStatus:
    """Collapse the staged statuses onto the original four.

    The pre-pipeline API contract (and the current frontend) only understands
    pending/running/completed/failed, so granular stages are reported through
    ``stage`` and the new progress endpoint instead.
    """
    return LEGACY_SEARCH_STATUSES.get(status, status)


def _lead_count(job: SearchJob, db: Session | None) -> int:
    """Count leads without loading them all into memory when a session is available."""
    if db is not None:
        return int(
            db.query(func.count(LeadRecord.id))
            .filter(LeadRecord.search_job_id == job.id)
            .scalar()
            or 0
        )
    return len(job.leads) if job.leads else 0


def activity_labels(db: Session, jobs: list[SearchJob]) -> dict[int, str]:
    """Say when email lookup is still queued after discovery has already finished."""
    if not jobs:
        return {}
    ids = {job.id for job in jobs}
    rows = (
        db.query(QueuedJob)
        .filter(QueuedJob.job_type == JobType.SEARCH_ENRICHMENT.value)
        .order_by(QueuedJob.id.desc())
        .all()
    )
    latest: dict[int, QueuedJob] = {}
    for row in rows:
        try:
            search_id = int((row.payload or {}).get("search_job_id") or 0)
        except (TypeError, ValueError):
            continue
        if search_id in ids and search_id not in latest:
            latest[search_id] = row
    labels: dict[int, str] = {}
    for job in jobs:
        queued = latest.get(job.id)
        if job.status == SearchJobStatus.ENRICHING or (queued and queued.status == "running"):
            labels[job.id] = "Finding emails"
        elif job.status in TERMINAL_SEARCH_STATUSES and queued and queued.status == "queued":
            labels[job.id] = (
                "Email lookup waiting to retry" if (queued.last_error or "").strip() else "Email lookup queued"
            )
    return labels


def search_job_out(
    job: SearchJob, db: Session | None = None, *, activity_label: str | None = None
) -> SearchJobOut:
    filters = dict(job.filters or {})
    # Progress used to be smuggled through the filters blob; drop the stale key so
    # it never leaks back out to clients.
    live_found = int(filters.pop("_found", 0) or 0)
    leads = _lead_count(job, db)
    qualified = int(job.qualified_leads or 0)
    candidates = int(job.candidates_found or 0)

    return SearchJobOut(
        id=job.id,
        name=job.name,
        prompt=job.prompt,
        filters=filters,
        linkedin_url=job.linkedin_url,
        query_used=job.query_used,
        mode=job.mode,
        status=legacy_status(job.status),
        detailed_status=job.status,
        stage=job.current_stage,
        message=job.message,
        progress=int(getattr(job, "progress", 0) or 0),
        found_count=leads or qualified or candidates or live_found,
        lead_count=leads,
        candidates_found=candidates,
        qualified_leads=qualified or leads,
        emails_found=int(job.emails_found or 0),
        phones_found=int(getattr(job, "phones_found", 0) or 0),
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        activity_label=activity_label,
    )


def _latest_enrichment_queue(db: Session, search_id: int) -> QueuedJob | None:
    rows = (
        db.query(QueuedJob)
        .filter(QueuedJob.job_type == JobType.SEARCH_ENRICHMENT.value)
        .order_by(QueuedJob.id.desc())
        .all()
    )
    for row in rows:
        payload = row.payload or {}
        try:
            if int(payload.get("search_job_id") or 0) == int(search_id):
                return row
        except (TypeError, ValueError):
            continue
    return None


def _manual_email_job(db: Session | None, search_id: int) -> ManualEmailJob | None:
    if db is None:
        return None
    from backend.services.EmailModule.jobs import latest_email_job

    row: EmailEnrichmentJob | None = latest_email_job(db, search_id)
    if row is None:
        return None
    return ManualEmailJob(
        status=row.status or "",
        processed=int(row.processed or 0),
        total=int(row.total or 0),
        found=int(row.found or 0),
        inferred=int(row.inferred or 0),
        verified=int(row.verified or 0),
        not_found=int(row.not_found or 0),
        message=row.message or "",
    )


def search_run_for(job: SearchJob, db: Session | None) -> SearchRunOut:
    queue = _latest_enrichment_queue(db, job.id) if db is not None else None
    manual = _manual_email_job(db, job.id)
    run = build_search_run(
        status=job.status.value if hasattr(job.status, "value") else job.status,
        enrichment_mode=(job.filters or {}).get("enrichment_mode"),
        total_queries=int(job.total_queries or 0),
        completed_queries=int(job.completed_queries or 0),
        failed_queries=int(job.failed_queries or 0),
        qualified_leads=int(job.qualified_leads or 0),
        enrichment_attempted=int(job.enrichment_attempted or 0),
        enrichment_completed=int(job.enrichment_completed or 0),
        emails_found=int(job.emails_found or 0),
        cancel_requested=bool(job.cancel_requested),
        job_message=job.message or "",
        queue_status=queue.status if queue is not None else None,
        queue_error=queue.last_error if queue is not None else None,
        manual=manual,
    )
    return SearchRunOut.model_validate(run.as_dict())


def search_progress_out(job: SearchJob, db: Session | None = None) -> SearchProgressOut:
    """Full progress payload for the polling endpoint."""
    return SearchProgressOut(
        id=job.id,
        name=job.name,
        status=job.status,
        legacy_status=legacy_status(job.status),
        current_stage=job.current_stage,
        progress=int(job.progress or 0),
        message=job.message or "",
        error_message=job.error_message,
        cancel_requested=bool(job.cancel_requested),
        run=search_run_for(job, db),
        lead_count=_lead_count(job, db),
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        **search_state.counters(job),
    )


def ensure_search_ready(job: SearchJob) -> None:
    if job.status == SearchJobStatus.PENDING:
        raise ValueError("Search has not started yet")
    if job.status not in TERMINAL_SEARCH_STATUSES:
        raise ValueError("Search is still running")
    if job.status == SearchJobStatus.FAILED:
        raise ValueError(job.message or "Search failed")
    if job.status == SearchJobStatus.CANCELLED:
        raise ValueError("Search was cancelled")
