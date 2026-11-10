"""Promote qualified candidates into ``LeadRecord`` rows.

Leads are written without contact details. Email and company enrichment happen
later in their own queued job, so users see relevant leads immediately.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import (
    CandidateStatus,
    EnrichmentStatus,
    LeadRecord,
    SearchCandidate,
    SearchJob,
)
from backend.observability import log_event
from backend.services.lead_enrichment import to_full_lead

logger = logging.getLogger(__name__)

BATCH_SIZE = 200


def _lead_from_candidate(job: SearchJob, candidate: SearchCandidate) -> LeadRecord:
    """Reuse the existing presentation enrichment (AI summary, work field, phone)."""
    base = to_full_lead(
        {
            "name": candidate.name or "",
            "title": candidate.title or "",
            "company": candidate.company or "",
            "location": candidate.location or "",
            "linkedin_url": candidate.linkedin_url or "",
            "website": candidate.linkedin_url or "",
            "snippet": candidate.raw_snippet or "",
            "source": candidate.source_url or "",
            "relevance_score": candidate.relevance_score or 0.0,
            "activity_status": (candidate.filter_match or {}).get("activity_status") or "",
            "tags": [],
        },
        prompt=job.prompt,
        filters=dict(job.filters or {}),
        allow_openai=False,
    )
    email = base.get("email")
    phone = base.get("phone")
    fm = dict(candidate.filter_match or {})
    scores = dict(fm.get("scores") or {})
    if email:
        scores["email_confidence"] = 0.85
    return LeadRecord(
        search_job_id=job.id,
        candidate_id=candidate.id,
        identity_key=candidate.identity_key,
        linkedin_url_normalized=candidate.linkedin_url_normalized,
        name=base.get("name") or candidate.name or "Unknown",
        email=email or None,
        email_status="found" if email else None,
        email_source="snippet" if email else None,
        phone=phone or None,
        phone_source="snippet" if phone else None,
        personal_location=base.get("personal_location") or candidate.location,
        ai_description=base.get("ai_description"),
        work_field=base.get("work_field"),
        work_designation=base.get("work_designation") or candidate.title,
        company_name=base.get("company_name") or candidate.company,
        company_location=base.get("company_location"),
        linkedin_url=candidate.linkedin_url,
        relevance_score=float(candidate.relevance_score or 0.0),
        activity_status=base.get("activity_status"),
        raw_snippet=candidate.raw_snippet,
        tags=base.get("tags") or [],
        enrichment_status=EnrichmentStatus.PENDING.value,
        score_breakdown=scores or None,
    )


def promote_qualified(db: Session, job: SearchJob) -> int:
    """Create a LeadRecord for every qualified candidate that lacks one.

    Returns the total number of leads now attached to the search.
    """
    existing_keys = {
        key
        for (key,) in db.query(LeadRecord.identity_key)
        .filter(LeadRecord.search_job_id == job.id, LeadRecord.identity_key.isnot(None))
        .all()
    }

    offset = 0
    while True:
        batch = (
            db.query(SearchCandidate)
            .filter(
                SearchCandidate.search_job_id == job.id,
                SearchCandidate.status == CandidateStatus.QUALIFIED.value,
            )
            .order_by(SearchCandidate.relevance_score.desc(), SearchCandidate.id)
            .offset(offset)
            .limit(BATCH_SIZE)
            .all()
        )
        if not batch:
            break
        offset += len(batch)

        for candidate in batch:
            if candidate.identity_key in existing_keys:
                continue
            from backend.services.discovery_engine.distribution import canonical_key_for, customer_key_for, try_allocate

            canonical = canonical_key_for(
                profile_url=candidate.linkedin_url or "",
                name=candidate.name or "",
                domain="",
            )
            if canonical is not None:
                decision = try_allocate(
                    db,
                    canonical_key=canonical,
                    customer_key=customer_key_for(job),
                    person_id=None,
                    search_job_id=job.id,
                    lead_record_id=None,
                )
                if decision == "saturated":
                    continue
            existing_keys.add(candidate.identity_key)
            db.add(_lead_from_candidate(job, candidate))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            log_event(
                logger,
                "promote.conflict",
                level=logging.WARNING,
                trace_id=job.trace_id,
                search_id=job.id,
            )

    return (
        db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).count()
    )
