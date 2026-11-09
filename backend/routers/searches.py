from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user, require_org_access
from backend.filters import FILTER_DEFINITIONS
from backend.models import EmailEnrichmentJob, LeadRecord, SearchJob, SearchJobStatus, User
from pydantic import BaseModel
from backend.schemas import EmailJobOut, LeadOut, SearchCreate, SearchJobOut, SearchResultOut
from backend.services.EmailModule.jobs import (
    latest_email_job,
    spawn_email_job,
    start_email_job,
)
from backend.services.export_service import leads_to_csv, leads_to_xml
from backend.services.search_jobs import enqueue_discovery
from backend.services.search_presenter import ensure_search_ready, search_job_out
from backend.services.search_service import create_search_job, run_search

router = APIRouter(prefix="/api", tags=["searches"])


def _email_job_out(job: EmailEnrichmentJob | None) -> EmailJobOut | None:
    if not job:
        return None
    return EmailJobOut.model_validate(job)


def _search_result(job: SearchJob, *, leads=None, message: str | None = None, db: Session | None = None) -> SearchResultOut:
    rows = leads if leads is not None else [LeadOut.model_validate(l) for l in job.leads]
    email_job = _email_job_out(latest_email_job(db, job.id) if db is not None else None)
    return SearchResultOut(
        search=search_job_out(job),
        leads=rows,
        query_used=job.query_used or "",
        count=len(rows),
        message=message if message is not None else (job.message or ""),
        mode=job.mode,
        email_job=email_job,
    )


@router.get("/filters")
def list_filters():
    return {"filters": FILTER_DEFINITIONS}


@router.post("/searches", response_model=SearchResultOut)
def create_search(
    body: SearchCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.filters.age_min and body.filters.age_max and body.filters.age_min > body.filters.age_max:
        raise HTTPException(status_code=400, detail="age_min cannot exceed age_max")

    org_id = body.organization_id
    if org_id:
        require_org_access(db, user, org_id, allow_viewer=False)

    if not body.save_results:
        result = run_search(
            db,
            user=user,
            name=body.name.strip(),
            prompt=body.prompt.strip(),
            filters=body.filters.model_dump(),
            organization_id=org_id,
            save_results=False,
        )
        search_out = SearchJobOut(
            id=0,
            name=body.name,
            prompt=body.prompt,
            filters=body.filters.model_dump(),
            linkedin_url=None,
            query_used=result["query_used"],
            mode=result["mode"],
            status=SearchJobStatus.COMPLETED,
            message=result["message"],
            progress=100,
            found_count=result["count"],
            lead_count=result["count"],
            created_at=__import__("datetime").datetime.utcnow(),
        )
        return SearchResultOut(
            search=search_out,
            leads=result["leads"],
            query_used=result["query_used"],
            count=result["count"],
            message=result["message"],
            mode=result["mode"],
        )

    job = create_search_job(
        db,
        user=user,
        name=body.name.strip(),
        prompt=body.prompt.strip(),
        filters=body.filters.model_dump(),
        organization_id=org_id,
    )
    # Queued rather than run in-request: a worker picks it up and the HTTP call
    # returns immediately with a job id to poll.
    enqueue_discovery(db, job)
    return SearchResultOut(
        search=search_job_out(job),
        leads=[],
        query_used=job.query_used or "",
        count=0,
        message=job.message or "Queued — discovery is starting…",
        mode=job.mode,
    )


@router.get("/searches", response_model=list[SearchJobOut])
def list_searches(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    jobs = db.query(SearchJob).filter(SearchJob.owner_id == user.id).order_by(SearchJob.created_at.desc()).all()
    return [search_job_out(j) for j in jobs]


@router.get("/searches/{search_id}", response_model=SearchResultOut)
def get_search(search_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = db.query(SearchJob).filter(SearchJob.id == search_id, SearchJob.owner_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Search not found")

    if job.status in {SearchJobStatus.PENDING, SearchJobStatus.RUNNING}:
        return SearchResultOut(
            search=search_job_out(job),
            leads=[],
            query_used=job.query_used or "",
            count=0,
            message=job.message or "Search in progress…",
            mode=job.mode,
            email_job=_email_job_out(latest_email_job(db, job.id)),
        )

    if job.status == SearchJobStatus.FAILED:
        return SearchResultOut(
            search=search_job_out(job),
            leads=[],
            query_used=job.query_used or "",
            count=0,
            message=job.message or "Search failed",
            mode=job.mode,
            email_job=_email_job_out(latest_email_job(db, job.id)),
        )

    return _search_result(job, db=db    )


@router.post("/searches/{search_id}/emails", response_model=SearchResultOut)
def start_search_emails(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(SearchJob).filter(SearchJob.id == search_id, SearchJob.owner_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Search not found")
    try:
        ensure_search_ready(job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    current = latest_email_job(db, search_id)
    if current and current.status == "running":
        return _search_result(job, db=db, message=current.message or "Email search already running")

    leads = db.query(LeadRecord).filter(LeadRecord.search_job_id == search_id).all()
    if not leads:
        raise HTTPException(status_code=400, detail="No leads to enrich")
    email_job = start_email_job(db, search_id, len(leads))
    spawn_email_job(email_job.id, [lead.id for lead in leads], use_providers=True)
    return _search_result(job, db=db, message="Finding emails in the background…")


@router.get("/searches/{search_id}/emails", response_model=SearchResultOut)
def get_search_emails(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(SearchJob).filter(SearchJob.id == search_id, SearchJob.owner_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Search not found")
    return _search_result(job, db=db)


@router.post("/leads/{lead_id}/email", response_model=LeadOut)
def enrich_one_lead(
    lead_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lead = (
        db.query(LeadRecord)
        .join(SearchJob)
        .filter(LeadRecord.id == lead_id, SearchJob.owner_id == user.id)
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    from backend.services.EmailModule.jobs import enrich_lead_record

    enrich_lead_record(db, lead, use_providers=True, use_public_web=False)
    return LeadOut.model_validate(lead)


class SearchRename(BaseModel):
    name: str


@router.patch("/searches/{search_id}", response_model=SearchJobOut)
def rename_search(
    search_id: int,
    body: SearchRename,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(SearchJob).filter(SearchJob.id == search_id, SearchJob.owner_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Search not found")
    job.name = body.name.strip() or job.name
    db.commit()
    db.refresh(job)
    return search_job_out(job)


@router.delete("/searches/{search_id}")
def delete_search(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(SearchJob).filter(SearchJob.id == search_id, SearchJob.owner_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Search not found")
    db.delete(job)
    db.commit()
    return {"ok": True, "deleted_id": search_id}


@router.post("/searches/{search_id}/duplicate", response_model=SearchResultOut)
def duplicate_and_rerun_search(
    search_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    src = db.query(SearchJob).filter(SearchJob.id == search_id, SearchJob.owner_id == user.id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Search not found")
    job = create_search_job(
        db,
        user=user,
        name=f"{src.name} (rerun)",
        prompt=src.prompt,
        filters=dict(src.filters or {}),
        organization_id=src.organization_id,
    )
    enqueue_discovery(db, job)
    return SearchResultOut(
        search=search_job_out(job),
        leads=[],
        query_used=job.query_used or "",
        count=0,
        message="Queued — discovery is starting…",
        mode=job.mode,
    )


@router.get("/searches/{search_id}/export")
def export_search(
    search_id: int,
    format: str = "csv",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(SearchJob).filter(SearchJob.id == search_id, SearchJob.owner_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Search not found")

    try:
        ensure_search_ready(job)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    leads = db.query(LeadRecord).filter(LeadRecord.search_job_id == search_id).all()
    rows = [
        {
            "name": l.name,
            "email": l.email,
            "email_status": l.email_status,
            "email_confidence": l.email_confidence,
            "email_source": l.email_source,
            "phone": l.phone,
            "personal_location": l.personal_location,
            "ai_description": l.ai_description,
            "work_field": l.work_field,
            "work_designation": l.work_designation,
            "company_name": l.company_name,
            "company_location": l.company_location,
            "linkedin_url": l.linkedin_url,
            "relevance_score": l.relevance_score,
            "activity_status": l.activity_status,
            "raw_snippet": l.raw_snippet,
        }
        for l in leads
    ]
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", job.name)[:40]
    if format.lower() == "xml":
        content = leads_to_xml(rows)
        return Response(
            content=content,
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.xml"'},
        )
    content = leads_to_csv(rows)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'},
    )
