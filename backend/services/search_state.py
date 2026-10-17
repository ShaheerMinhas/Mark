"""SearchJob lifecycle and progress accounting.

All state the frontend polls is written here, so progress always reflects work the
backend actually completed. Counters are real columns rather than values smuggled
through the ``filters`` JSON blob.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from backend.models import (
    OPEN_SEARCH_STATUSES,
    SearchJob,
    SearchJobStatus,
    TERMINAL_SEARCH_STATUSES,
)
from backend.observability import log_event

logger = logging.getLogger(__name__)

#: Nominal progress floor for each stage, so the bar never travels backwards.
STAGE_PROGRESS = {
    SearchJobStatus.PENDING: 1,
    SearchJobStatus.DISCOVERING: 5,
    SearchJobStatus.FILTERING: 75,
    SearchJobStatus.ENRICHING: 85,
}

COUNTER_FIELDS = (
    "total_queries",
    "completed_queries",
    "failed_queries",
    "candidates_found",
    "candidates_deduplicated",
    "qualified_leads",
    "enrichment_attempted",
    "enrichment_completed",
    "emails_found",
    "phones_found",
    "errors_count",
)

#: Shown to users in place of raw exception text.
GENERIC_FAILURE_MESSAGE = "Search could not be completed. Please try again."


class SearchCancelled(Exception):
    """Raised inside a stage when the owner has requested cancellation."""


def counters(job: SearchJob) -> dict[str, int]:
    return {field: int(getattr(job, field, 0) or 0) for field in COUNTER_FIELDS}


def is_open(job: SearchJob) -> bool:
    return job.status in OPEN_SEARCH_STATUSES


def is_terminal(job: SearchJob) -> bool:
    return job.status in TERMINAL_SEARCH_STATUSES


def begin_stage(
    db: Session,
    job: SearchJob,
    status: SearchJobStatus,
    stage: str,
    *,
    message: str | None = None,
) -> None:
    job.status = status
    job.current_stage = stage
    if message is not None:
        job.message = message
    floor = STAGE_PROGRESS.get(status)
    if floor is not None:
        job.progress = max(int(job.progress or 0), floor)
    if job.started_at is None:
        job.started_at = datetime.utcnow()
    db.commit()
    log_event(logger, "stage.begin", trace_id=job.trace_id, search_id=job.id, stage=stage)


def bump(db: Session, job: SearchJob, **deltas: int) -> None:
    """Increment counters. Unknown names are ignored rather than raising mid-stage."""
    for field, delta in deltas.items():
        if field not in COUNTER_FIELDS or not delta:
            continue
        setattr(job, field, int(getattr(job, field, 0) or 0) + int(delta))
    db.commit()


def set_progress(
    db: Session,
    job: SearchJob,
    *,
    progress: int | None = None,
    message: str | None = None,
    stage: str | None = None,
) -> None:
    if progress is not None:
        job.progress = max(int(job.progress or 0), min(99, int(progress)))
    if message is not None:
        job.message = message
    if stage is not None:
        job.current_stage = stage
    db.commit()


def discovery_progress(job: SearchJob) -> int:
    """Map completed queries onto the discovery band of the progress bar."""
    total = int(job.total_queries or 0)
    if total <= 0:
        return STAGE_PROGRESS[SearchJobStatus.DISCOVERING]
    done = int(job.completed_queries or 0) + int(job.failed_queries or 0)
    span = STAGE_PROGRESS[SearchJobStatus.FILTERING] - STAGE_PROGRESS[SearchJobStatus.DISCOVERING]
    return STAGE_PROGRESS[SearchJobStatus.DISCOVERING] + int(span * min(1.0, done / total))


def check_cancelled(db: Session, job: SearchJob) -> None:
    """Raise :class:`SearchCancelled` if the owner asked to stop."""
    db.refresh(job, attribute_names=["cancel_requested"])
    if job.cancel_requested:
        raise SearchCancelled


def request_cancel(db: Session, job: SearchJob) -> bool:
    if is_terminal(job):
        return False
    job.cancel_requested = True
    db.commit()
    log_event(logger, "search.cancel_requested", trace_id=job.trace_id, search_id=job.id)
    return True


def finish(
    db: Session,
    job: SearchJob,
    status: SearchJobStatus,
    *,
    message: str,
    error_message: str | None = None,
) -> None:
    job.status = status
    job.message = message
    job.error_message = error_message
    job.current_stage = None
    job.progress = 100
    job.completed_at = datetime.utcnow()
    db.commit()
    log_event(
        logger,
        "search.finish",
        trace_id=job.trace_id,
        search_id=job.id,
        status=status.value,
        duration_ms=_duration_ms(job),
        **counters(job),
    )


def fail(db: Session, job: SearchJob, *, reason: str, detail: str = "") -> None:
    """Terminate a search with a user-safe message; detail only reaches the logs."""
    job.errors_count = int(job.errors_count or 0) + 1
    log_event(
        logger,
        "search.failed",
        level=logging.ERROR,
        trace_id=job.trace_id,
        search_id=job.id,
        reason=reason,
        detail=(detail or "")[:500],
    )
    finish(db, job, SearchJobStatus.FAILED, message=GENERIC_FAILURE_MESSAGE, error_message=reason)


def _duration_ms(job: SearchJob) -> int | None:
    if not job.started_at or not job.completed_at:
        return None
    return int((job.completed_at - job.started_at).total_seconds() * 1000)
