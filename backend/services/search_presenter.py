from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models import (
    LEGACY_SEARCH_STATUSES,
    LeadRecord,
    SearchJob,
    SearchJobStatus,
    TERMINAL_SEARCH_STATUSES,
)
from backend.schemas import SearchJobOut, SearchProgressOut
from backend.services import search_state


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


def search_job_out(job: SearchJob, db: Session | None = None) -> SearchJobOut:
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
    )


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
