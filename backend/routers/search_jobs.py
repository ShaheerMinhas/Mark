"""Progressive, paginated search APIs.

Status and results are deliberately separate endpoints: the frontend polls status
cheaply and only fetches a page of leads when the counters say there is something
new. No endpoint here returns an unbounded list.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models import (
    CandidateStatus,
    EnrichmentStatus,
    LeadRecord,
    QueryRunStatus,
    SearchCandidate,
    SearchJob,
    SearchQueryRun,
    User,
)
from backend.schemas import (
    CandidateOut,
    CandidatePageOut,
    LeadOut,
    LeadPageOut,
    PageMeta,
    SearchFunnelOut,
    SearchProgressOut,
)
from backend.services import search_state
from backend.services.discovery.qualify import rejection_breakdown
from backend.services.search_presenter import search_progress_out

router = APIRouter(prefix="/api/search-jobs", tags=["search-jobs"])

MAX_PAGE_SIZE = 200

LeadSort = Literal["relevance", "recent", "name", "company"]

_LEAD_SORTS = {
    "relevance": (LeadRecord.relevance_score.desc(), LeadRecord.id.asc()),
    "recent": (LeadRecord.created_at.desc(), LeadRecord.id.desc()),
    "name": (LeadRecord.name.asc(), LeadRecord.id.asc()),
    "company": (LeadRecord.company_name.asc(), LeadRecord.id.asc()),
}


def _owned_job(db: Session, search_id: int, user: User) -> SearchJob:
    job = (
        db.query(SearchJob)
        .filter(SearchJob.id == search_id, SearchJob.owner_id == user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Search not found")
    return job


def _page_meta(page: int, page_size: int, total: int) -> PageMeta:
    total_pages = max(1, (total + page_size - 1) // page_size)
    return PageMeta(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        has_next=page < total_pages,
    )


@router.get("/{search_id}", response_model=SearchProgressOut)
def get_search_progress(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Status, stage and counters. Cheap enough to poll every second."""
    return search_progress_out(_owned_job(db, search_id, user), db)


@router.get("/{search_id}/leads", response_model=LeadPageOut)
def list_search_leads(
    search_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort: LeadSort = "relevance",
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    has_email: bool | None = None,
    search: str | None = Query(None, max_length=120),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One page of qualified leads. Available while the search is still running."""
    job = _owned_job(db, search_id, user)

    query = db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id)
    if min_score:
        query = query.filter(LeadRecord.relevance_score >= min_score)
    if has_email is True:
        query = query.filter(LeadRecord.email.isnot(None))
    elif has_email is False:
        query = query.filter(LeadRecord.email.is_(None))
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(
            LeadRecord.name.ilike(pattern)
            | LeadRecord.company_name.ilike(pattern)
            | LeadRecord.work_designation.ilike(pattern)
        )

    total = int(query.with_entities(func.count(LeadRecord.id)).scalar() or 0)
    rows = (
        query.order_by(*_LEAD_SORTS[sort])
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return LeadPageOut(
        items=[LeadOut.model_validate(row) for row in rows],
        page=_page_meta(page, page_size, total),
    )


@router.get("/{search_id}/candidates", response_model=CandidatePageOut)
def list_search_candidates(
    search_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    status: CandidateStatus | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The raw discovery pool, including rejected candidates and why they were rejected."""
    job = _owned_job(db, search_id, user)

    query = db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id)
    if status is not None:
        query = query.filter(SearchCandidate.status == status.value)

    total = int(query.with_entities(func.count(SearchCandidate.id)).scalar() or 0)
    rows = (
        query.order_by(SearchCandidate.relevance_score.desc(), SearchCandidate.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return CandidatePageOut(
        items=[CandidateOut.model_validate(row) for row in rows],
        page=_page_meta(page, page_size, total),
    )


@router.get("/{search_id}/funnel", response_model=SearchFunnelOut)
def get_search_funnel(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Candidates vs relevant vs enriched vs contactable, kept distinct."""
    job = _owned_job(db, search_id, user)

    def lead_count(*conditions) -> int:
        query = db.query(func.count(LeadRecord.id)).filter(
            LeadRecord.search_job_id == job.id
        )
        for condition in conditions:
            query = query.filter(condition)
        return int(query.scalar() or 0)

    candidates = int(
        db.query(func.count(SearchCandidate.id))
        .filter(SearchCandidate.search_job_id == job.id)
        .scalar()
        or 0
    )
    query_outcomes = {
        str(status): int(count)
        for status, count in db.query(SearchQueryRun.status, func.count(SearchQueryRun.id))
        .filter(SearchQueryRun.search_job_id == job.id)
        .group_by(SearchQueryRun.status)
        .all()
    }
    for status in QueryRunStatus:
        query_outcomes.setdefault(status.value, 0)

    return SearchFunnelOut(
        candidates_found=candidates or int(job.candidates_found or 0),
        relevant_leads=lead_count(),
        linkedin_leads=lead_count(LeadRecord.linkedin_url.isnot(None)),
        enriched_leads=lead_count(
            LeadRecord.enrichment_status.in_(
                [EnrichmentStatus.ENRICHED.value, EnrichmentStatus.NO_MATCH.value]
            )
        ),
        contactable_leads=lead_count(LeadRecord.email.isnot(None)),
        phoneable_leads=lead_count(LeadRecord.phone.isnot(None)),
        rejections=rejection_breakdown(db, job.id),
        query_outcomes=query_outcomes,
    )


@router.post("/{search_id}/cancel", response_model=SearchProgressOut)
def cancel_search(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ask the worker to stop. Candidates discovered so far are kept."""
    job = _owned_job(db, search_id, user)
    if not search_state.request_cancel(db, job):
        raise HTTPException(status_code=409, detail="Search has already finished")
    return search_progress_out(job, db)


@router.post("/{search_id}/enrich", response_model=SearchProgressOut)
def retry_enrichment(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Queue another enrichment pass. Leads that failed or hit a 429 are retried."""
    from backend.services.search_jobs import enqueue_enrichment

    job = _owned_job(db, search_id, user)
    retryable = (
        db.query(LeadRecord)
        .filter(
            LeadRecord.search_job_id == job.id,
            LeadRecord.enrichment_status.in_(
                [
                    EnrichmentStatus.FAILED.value,
                    EnrichmentStatus.RATE_LIMITED.value,
                    EnrichmentStatus.PENDING.value,
                ]
            ),
        )
        .update(
            {
                LeadRecord.enrichment_status: EnrichmentStatus.PENDING.value,
                LeadRecord.enrichment_locked_until: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not retryable:
        raise HTTPException(status_code=409, detail="Nothing left to enrich")
    enqueue_enrichment(db, job)
    return search_progress_out(job, db)
