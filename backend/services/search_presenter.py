from __future__ import annotations

from backend.models import SearchJob, SearchJobStatus
from backend.schemas import SearchJobOut


def search_job_out(job: SearchJob) -> SearchJobOut:
    filters = dict(job.filters or {})
    live_found = int(filters.pop("_found", 0) or 0)
    return SearchJobOut(
        id=job.id,
        name=job.name,
        prompt=job.prompt,
        filters=filters,
        linkedin_url=job.linkedin_url,
        query_used=job.query_used,
        mode=job.mode,
        status=job.status,
        message=job.message,
        progress=int(getattr(job, "progress", 0) or 0),
        found_count=len(job.leads) if job.leads else live_found,
        lead_count=len(job.leads),
        created_at=job.created_at,
    )


def ensure_search_ready(job: SearchJob) -> None:
    if job.status == SearchJobStatus.PENDING:
        raise ValueError("Search has not started yet")
    if job.status == SearchJobStatus.RUNNING:
        raise ValueError("Search is still running")
    if job.status == SearchJobStatus.FAILED:
        raise ValueError(job.message or "Search failed")
