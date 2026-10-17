from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.lead_finder import search_leads
from backend.models import LeadRecord, SearchJob, SearchJobStatus, User
from backend.observability import new_trace_id
from backend.services.lead_enrichment import to_full_lead
from backend.services.pipeline import enrich_leads

_ENRICH_SECONDS = 20.0


def _set_status(db: Session, job: SearchJob, status: SearchJobStatus, message: str | None = None) -> None:
    job.status = status
    if message is not None:
        job.message = message
    db.commit()
    db.refresh(job)


def _set_progress(db: Session, job: SearchJob, pct: int, stage: str, found: int = 0) -> None:
    job.progress = max(0, min(99, int(pct)))
    job.message = f"{stage} · {found} people so far" if found else stage
    filters = dict(job.filters or {})
    filters["_found"] = int(found)
    job.filters = filters
    flag_modified(job, "filters")
    db.commit()


def create_search_job(
    db: Session,
    *,
    user: User,
    name: str,
    prompt: str,
    filters: dict[str, Any],
    organization_id: int | None,
) -> SearchJob:
    job = SearchJob(
        name=name,
        prompt=prompt,
        filters=filters,
        linkedin_url=None,
        status=SearchJobStatus.PENDING,
        progress=1,
        message="Queued — discovery is starting…",
        owner_id=user.id,
        organization_id=organization_id,
        trace_id=new_trace_id(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def run_search(
    db: Session,
    *,
    user: User,
    name: str = "",
    prompt: str = "",
    filters: dict[str, Any] | None = None,
    organization_id: int | None = None,
    save_results: bool = True,
    job: SearchJob | None = None,
) -> dict[str, Any]:
    """Public web search only — no LinkedIn login or session required."""
    search_job = job
    if search_job is None:
        f = filters or {}
        prompt = prompt or ""
        if save_results:
            search_job = create_search_job(
                db,
                user=user,
                name=name,
                prompt=prompt,
                filters=f,
                organization_id=organization_id,
            )
        else:
            f = f
    else:
        f = dict(search_job.filters or {})
        prompt = search_job.prompt
        name = search_job.name

    def on_people_progress(pct: int, stage: str, found: int) -> None:
        if search_job:
            _set_progress(db, search_job, pct, stage, found)

    try:
        if search_job:
            _set_status(db, search_job, SearchJobStatus.RUNNING, "Starting a 2-minute public search…")
            search_job.progress = 4
            db.commit()

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
            max_results=int(f.get("max_results") or 100),
            active_only=bool(f.get("active_only", True)),
            business_only=bool(f.get("business_only", True)),
            on_progress=on_people_progress,
        )

        raw_leads = result.get("leads") or []
        mode = result.get("mode") or "public"
        message = result.get("message") or ""
        query_used = result.get("query_used") or ""

        enriched: list[dict[str, Any]] = []
        if raw_leads:
            if search_job:
                _set_progress(db, search_job, 90, "Reading locations from profiles", len(raw_leads))
            enriched = [to_full_lead(r, prompt=prompt, filters=f) for r in raw_leads]

            def on_enrich_progress(pct: int, stage: str, found: int) -> None:
                if search_job:
                    _set_progress(db, search_job, pct, stage, found)

            enriched, extras = enrich_leads(
                enriched,
                budget_seconds=_ENRICH_SECONDS,
                on_progress=on_enrich_progress,
                found=len(raw_leads),
            )
            bits = [
                f"{extras.get('people_locations', 0)} people location(s)",
                f"{extras.get('locations', 0)} company location(s)",
                f"{extras.get('emails', 0)} public email(s)",
            ]
            message = f"{message} Enriched with {', '.join(bits)}."

        if search_job:
            search_job.query_used = query_used
            search_job.mode = mode
            search_job.message = message
            search_job.progress = 100
            filters_clean = dict(search_job.filters or {})
            filters_clean.pop("_found", None)
            search_job.filters = filters_clean
            flag_modified(search_job, "filters")

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
                        company_domain=lead.get("company_domain"),
                        email_status=lead.get("email_status"),
                        email_confidence=lead.get("email_confidence"),
                        email_source=lead.get("email_source"),
                        email_pattern=lead.get("email_pattern"),
                        email_source_url=lead.get("email_source_url"),
                        email_evidence=lead.get("email_evidence"),
                        email_verification=lead.get("email_verification"),
                    )
                )

            _set_status(db, search_job, SearchJobStatus.COMPLETED, message)
            db.refresh(search_job)
            for i, rec in enumerate(search_job.leads):
                if i < len(enriched):
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
            search_job.progress = 100
            _set_status(db, search_job, SearchJobStatus.FAILED, str(exc))
        raise
