"""Read the shared public cache. Writes stay in the service layer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models import User
from backend.services.global_cache.match import CacheQuery, match_people
from backend.services.global_cache.normalize import country_code, normalize_domain, normalize_technology

router = APIRouter(prefix="/api/cache", tags=["cache"])


def _csv(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


@router.get("/people")
def list_cached_people(
    country: str = "",
    industry: str = "",
    seniority: str = "",
    function: str = "",
    title: str = "",
    domain: str = "",
    technology: str = "",
    email_status: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Server-side match. Authenticated users see public cache rows, not each other's leads."""
    del user  # authentication is the gate; rows are not workspace-owned
    codes = []
    for raw in _csv(country):
        code = country_code(raw) or (raw.upper() if len(raw) == 2 else "")
        if code and code not in codes:
            codes.append(code)
    query = CacheQuery(
        country_codes=codes,
        industries=_csv(industry),
        seniorities=[item.lower() for item in _csv(seniority)],
        functions=[item.lower() for item in _csv(function)],
        titles=[normalize_technology(item) or "" for item in _csv(title)],
        domain=normalize_domain(domain),
        technologies=[normalize_technology(item) or "" for item in _csv(technology)],
        email_statuses=[item.lower() for item in _csv(email_status)],
        page=page,
        page_size=page_size,
    )
    query.titles = [item for item in query.titles if item]
    query.technologies = [item for item in query.technologies if item]
    total, rows = match_people(db, query)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": row.id,
                "full_name": row.full_name,
                "given_name": row.given_name,
                "family_name": row.family_name,
                "title": row.title,
                "job_function": row.job_function,
                "department": row.department,
                "seniority": row.seniority,
                "company_name": row.company_name,
                "domain": row.domain,
                "industry": row.industry,
                "subindustry": row.subindustry,
                "employee_count": row.employee_count,
                "size_band": row.size_band,
                "company_type": row.company_type,
                "country_code": row.country_code,
                "region": row.region,
                "city": row.city,
                "profile_url": row.profile_url,
                "email": row.email,
                "email_status": row.email_status,
                "email_confidence": row.email_confidence,
                "phone": row.phone,
                "quality_tier": row.quality_tier,
                "quality_score": row.quality_score,
                "updated_at": row.updated_at,
            }
            for row in rows
        ],
    }
