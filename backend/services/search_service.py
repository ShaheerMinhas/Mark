from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.lead_finder import search_leads
from backend.models import LeadRecord, SearchJob, SearchJobStatus, User
from backend.services.lead_enrichment import to_full_lead


def _set_status(db: Session, job: SearchJob, status: SearchJobStatus, message: str | None = None) -> None:
    job.status = status
    if message is not None:
        job.message = message
    db.commit()
    db.refresh(job)


def run_search(
    db: Session,
    *,
    user: User,
    name: str,
    prompt: str,
    filters: dict[str, Any],
    organization_id: int | None,
    save_results: bool,
) -> dict[str, Any]:
    """Public web search only — no LinkedIn login or session required."""
    f = filters
    search_job: SearchJob | None = None

    if save_results:
        search_job = SearchJob(
            name=name,
            prompt=prompt,
            filters=f,
            linkedin_url=None,
            status=SearchJobStatus.PENDING,
            owner_id=user.id,
            organization_id=organization_id,
        )
        db.add(search_job)
        db.commit()
        db.refresh(search_job)
        _set_status(db, search_job, SearchJobStatus.RUNNING)

    try:
        result = search_leads(
            prompt,
            location=str(f.get("location") or ""),
            industry=str(f.get("industry") or ""),
            company=str(f.get("company") or ""),
            interests=list(f.get("interests") or []),
            job_titles=list(f.get("job_titles") or []),
            company_size=str(f.get("company_size") or ""),
            income_level=str(f.get("income_level") or ""),
            gender="" if f.get("gender") in ("", "Any") else str(f.get("gender") or ""),
            age_min=f.get("age_min"),
            age_max=f.get("age_max"),
            language=str(f.get("language") or ""),
            max_results=int(f.get("max_results") or 25),
            active_only=bool(f.get("active_only", True)),
            business_only=bool(f.get("business_only", True)),
        )

        raw_leads = result.get("leads") or []
        mode = result.get("mode") or "public"
        message = result.get("message") or ""
        query_used = result.get("query_used") or ""

        enriched = [to_full_lead(r, prompt=prompt, filters=f) for r in raw_leads]

        if search_job:
            search_job.query_used = query_used
            search_job.mode = mode
            search_job.message = message

            for lead in enriched:
                db.add(
                    LeadRecord(
                        search_job_id=search_job.id,
                        name=lead["name"],
                        email=lead.get("email"),
                        phone=lead.get("phone"),
                        personal_location=lead.get("personal_location"),
                        ai_description=lead.get("ai_description"),
                        work_field=lead.get("work_field"),
                        work_designation=lead.get("work_designation"),
                        company_name=lead.get("company_name"),
                        company_location=lead.get("company_location"),
                        linkedin_url=lead.get("linkedin_url"),
                        relevance_score=lead.get("relevance_score") or 0,
                        activity_status=lead.get("activity_status"),
                        raw_snippet=lead.get("raw_snippet"),
                        tags=lead.get("tags") or [],
                    )
                )

            _set_status(db, search_job, SearchJobStatus.COMPLETED, message)
            db.refresh(search_job)
            for i, rec in enumerate(search_job.leads):
                enriched[i]["id"] = rec.id

        return {
            "search_id": search_job.id if search_job else None,
            "search_job": search_job,
            "leads": enriched,
            "query_used": query_used,
            "count": len(enriched),
            "message": message,
            "mode": mode,
            "status": search_job.status if search_job else SearchJobStatus.COMPLETED,
        }
    except Exception as exc:
        if search_job:
            _set_status(db, search_job, SearchJobStatus.FAILED, str(exc))
        raise
