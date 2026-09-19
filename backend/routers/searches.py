from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user, require_org_access
from backend.filters import FILTER_DEFINITIONS
from backend.models import LeadRecord, SearchJob, SearchJobStatus, User
from backend.schemas import LeadOut, SearchCreate, SearchJobOut, SearchResultOut
from backend.services.export_service import leads_to_csv, leads_to_xml
from backend.services.search_presenter import ensure_search_ready, search_job_out
from backend.services.search_service import run_search

router = APIRouter(prefix="/api", tags=["searches"])


@router.get("/filters")
def list_filters():
    return {"filters": FILTER_DEFINITIONS}


@router.post("/searches", response_model=SearchResultOut)
def create_search(body: SearchCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.filters.age_min and body.filters.age_max and body.filters.age_min > body.filters.age_max:
        raise HTTPException(status_code=400, detail="age_min cannot exceed age_max")

    org_id = body.organization_id
    if org_id:
        require_org_access(db, user, org_id)

    result = run_search(
        db,
        user=user,
        name=body.name.strip(),
        prompt=body.prompt.strip(),
        filters=body.filters.model_dump(),
        organization_id=org_id,
        save_results=body.save_results,
    )

    job = result.get("search_job")
    if job:
        search_out = search_job_out(job)
    else:
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
        )

    if job.status == SearchJobStatus.FAILED:
        return SearchResultOut(
            search=search_job_out(job),
            leads=[],
            query_used=job.query_used or "",
            count=0,
            message=job.message or "Search failed",
            mode=job.mode,
        )

    leads = [LeadOut.model_validate(l) for l in job.leads]
    return SearchResultOut(
        search=search_job_out(job),
        leads=leads,
        query_used=job.query_used or "",
        count=len(leads),
        message=job.message or "",
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
