"""Indexed cache search. Filtering happens in SQL, not by loading the table."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session, joinedload

from sqlalchemy import or_

from backend.models import DistributionSlot, GlobalPerson, GlobalPersonTechnology, LeadAllocation
from backend.services.global_cache.normalize import country_code, normalize_domain, normalize_technology


@dataclass
class CacheQuery:
    country_codes: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    seniorities: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    domain: str | None = None
    technologies: list[str] = field(default_factory=list)
    email_statuses: list[str] = field(default_factory=list)
    size_bands: list[str] = field(default_factory=list)
    updated_after: datetime | None = None
    page: int = 1
    page_size: int = 25
    customer_key: str | None = None
    max_recipients: int | None = None


def _filtered(db: Session, query: CacheQuery):
    from backend.services.discovery_engine.suppression import suppressed_people_filter

    q = suppressed_people_filter(db, db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None)))
    if query.country_codes:
        q = q.filter(GlobalPerson.country_code.in_(query.country_codes))
    if query.industries:
        q = q.filter(GlobalPerson.industry.in_(query.industries))
    if query.seniorities:
        q = q.filter(GlobalPerson.seniority.in_(query.seniorities))
    if query.functions:
        q = q.filter(GlobalPerson.job_function.in_(query.functions))
    if query.domain:
        q = q.filter(GlobalPerson.domain == query.domain)
    if query.email_statuses:
        q = q.filter(GlobalPerson.email_status.in_(query.email_statuses))
    if query.size_bands:
        q = q.filter(GlobalPerson.size_band.in_(query.size_bands))
    if query.updated_after is not None:
        q = q.filter(GlobalPerson.updated_at >= query.updated_after)
    if query.titles:
        clauses = [GlobalPerson.title_normalized.contains(title) for title in query.titles if title]
        if clauses:
            cond = clauses[0]
            for extra in clauses[1:]:
                cond = cond | extra
            q = q.filter(cond)
    if query.customer_key and query.max_recipients:
        mine = db.query(LeadAllocation.canonical_key).filter(LeadAllocation.customer_key == query.customer_key)
        q = q.outerjoin(DistributionSlot, DistributionSlot.canonical_key == GlobalPerson.canonical_key)
        q = q.filter(
            or_(
                DistributionSlot.recipient_count.is_(None),
                DistributionSlot.recipient_count < int(query.max_recipients),
                GlobalPerson.canonical_key.in_(mine),
            )
        )
    if query.technologies:
        q = q.filter(
            GlobalPerson.id.in_(
                db.query(GlobalPersonTechnology.person_id).filter(
                    GlobalPersonTechnology.technology.in_(query.technologies)
                )
            )
        )
    return q


def match_people(db: Session, query: CacheQuery) -> tuple[int, list[GlobalPerson]]:
    page = max(1, int(query.page or 1))
    size = max(1, min(100, int(query.page_size or 25)))
    filtered = _filtered(db, query)
    total = filtered.order_by(None).count()
    rows = (
        filtered.options(joinedload(GlobalPerson.company))
        .order_by(GlobalPerson.quality_score.desc(), GlobalPerson.updated_at.desc(), GlobalPerson.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return int(total), rows


def query_from_search_filters(filters: dict) -> CacheQuery:
    """Map a customer search onto cache fields. Unmapped places do not match a guessed country."""
    filters = filters or {}
    codes: list[str] = []
    for raw in (filters.get("country"), filters.get("location"), filters.get("company_location")):
        if not raw:
            continue
        code = country_code(str(raw))
        if code and code not in codes:
            codes.append(code)
    titles = []
    for title in filters.get("job_titles") or []:
        text = normalize_technology(str(title))
        if text:
            titles.append(text)
    industry = (filters.get("industry") or "").strip()
    domain = normalize_domain(filters.get("company") or "")
    return CacheQuery(
        country_codes=codes,
        industries=[industry] if industry else [],
        titles=titles,
        domain=domain,
        page=1,
        page_size=100,
    )
