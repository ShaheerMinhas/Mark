"""Copy public cache rows into one customer's search. The cache row stays shared."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.models import (
    CandidateStatus,
    EnrichmentStatus,
    LeadRecord,
    SearchCandidate,
    SearchJob,
)
from backend.services.global_cache.match import match_people, query_from_search_filters
from backend.services.identity import identity_key


@dataclass
class CacheCoverage:
    matched: int
    served: int
    requested: int
    sufficient: bool


def _requested(filters: dict) -> int:
    try:
        count = int((filters or {}).get("max_results") or 0)
    except (TypeError, ValueError):
        count = 0
    return count if count > 0 else 50


def serve_cached_people(db: Session, job: SearchJob) -> CacheCoverage:
    filters = dict(job.filters or {})
    requested = _requested(filters)
    from backend.services.discovery_engine.distribution import customer_key_for, ensure_policy

    query = query_from_search_filters(filters)
    query.customer_key = customer_key_for(job)
    query.max_recipients = int(ensure_policy(db).max_customers or 3)
    query.page_size = min(100, requested)
    matched, rows = match_people(db, query)
    existing = {
        key
        for (key,) in db.query(LeadRecord.identity_key)
        .filter(LeadRecord.search_job_id == job.id, LeadRecord.identity_key.isnot(None))
        .all()
    }
    served = 0
    from backend.services.discovery_engine.suppression import is_suppressed

    for person in rows:
        if is_suppressed(
            db,
            email=person.email,
            domain=person.domain,
            profile_url=person.profile_url_normalized or person.profile_url,
        ):
            continue
        if served >= requested:
            break
        key = identity_key(
            linkedin_url=person.profile_url or "",
            name=person.full_name or "",
            company=person.company_name or "",
            title=person.title or "",
        )
        if not key or key in existing:
            continue
        from backend.services.discovery_engine.distribution import canonical_key_for, customer_key_for, try_allocate

        canonical = canonical_key_for(
            profile_url=person.profile_url or "",
            name=person.full_name or "",
            domain=person.domain or "",
        )
        if canonical is None:
            continue
        decision = try_allocate(
            db,
            canonical_key=canonical,
            customer_key=customer_key_for(job),
            person_id=person.id,
            search_job_id=job.id,
            lead_record_id=None,
        )
        if decision == "saturated":
            continue
        existing.add(key)
        candidate = SearchCandidate(
            search_job_id=job.id,
            identity_key=key,
            name=person.full_name,
            title=person.title,
            company=person.company_name,
            location=person.city or person.country_code,
            linkedin_url=person.profile_url,
            linkedin_url_normalized=person.profile_url_normalized,
            source_url=person.profile_url,
            raw_snippet=None,
            status=CandidateStatus.QUALIFIED.value,
            relevance_score=float(person.quality_score or 0),
        )
        db.add(candidate)
        db.flush()
        db.add(
            LeadRecord(
                search_job_id=job.id,
                candidate_id=candidate.id,
                identity_key=key,
                linkedin_url_normalized=person.profile_url_normalized,
                name=person.full_name or "Unknown",
                email=person.email,
                email_status=person.email_status,
                email_confidence=person.email_confidence,
                email_source="shared_cache" if person.email else None,
                phone=person.phone,
                phone_source="shared_cache" if person.phone else None,
                personal_location=person.city,
                work_designation=person.title,
                work_field=person.job_function,
                company_name=person.company_name,
                company_domain=person.domain,
                company_location=person.country_code,
                linkedin_url=person.profile_url,
                relevance_score=float(person.quality_score or 0),
                enrichment_status=EnrichmentStatus.PENDING.value,
                tags=[],
            )
        )
        served += 1
    job.cache_served = int(job.cache_served or 0) + served
    if served:
        db.flush()
        job.candidates_found = int(job.candidates_found or 0) + served
        job.qualified_leads = db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).count()
    db.commit()
    return CacheCoverage(
        matched=matched,
        served=served,
        requested=requested,
        sufficient=served >= requested and served > 0,
    )
