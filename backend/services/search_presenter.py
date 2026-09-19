from __future__ import annotations

from backend.models import SearchJob, SearchJobStatus
from backend.schemas import SearchJobOut


def search_job_out(job: SearchJob) -> SearchJobOut:
    return SearchJobOut(
        id=job.id,
        name=job.name,
        prompt=job.prompt,
        filters=job.filters or {},
        linkedin_url=job.linkedin_url,
        query_used=job.query_used,
        mode=job.mode,
        status=job.status,
        message=job.message,
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
