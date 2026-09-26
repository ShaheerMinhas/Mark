"""Run capability modules after a people search.

Order: snippet geo → CompanyModule (shared per employer) → EmailModule (per person).
"""

from __future__ import annotations

import time
from typing import Any

from backend.config import HUNTER_API_KEY
from backend.services.CompanyModule import lookup_company
from backend.services.CompanyModule.extract import extract_profile_geo
from backend.services.CompanyModule.finder import is_usable_company
from backend.services.EmailModule.intelligence import EmailEnrichmentService
from backend.services.EmailModule.types import PersonInput
from backend.services.ddg import ddg_session


def enrich_leads(
    leads: list[dict[str, Any]],
    *,
    budget_seconds: float = 90.0,
    on_progress: Any | None = None,
    found: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fill company location/domain and public emails. Stops when the time budget is hit."""
    deadline = time.monotonic() + max(15.0, budget_seconds)
    companies_looked_up = 0
    found = found or len(leads)

    def report(pct: int, stage: str) -> None:
        if on_progress:
            on_progress(max(88, min(99, pct)), stage, found)

    for lead in leads:
        snippet = lead.get("raw_snippet") or ""
        geo = extract_profile_geo(snippet)
        if geo and not lead.get("personal_location"):
            lead["personal_location"] = geo
        company = (lead.get("company_name") or "").strip()
        if geo and is_usable_company(company) and not lead.get("company_location"):
            if company.lower() in snippet.lower():
                lead["company_location"] = geo

    report(90, "Reading locations from profiles")

    unique_companies: list[str] = []
    seen: set[str] = set()
    for lead in leads:
        name = (lead.get("company_name") or "").strip()
        key = name.lower()
        if name and key not in seen and is_usable_company(name):
            seen.add(key)
            unique_companies.append(name)

    located = {
        (lead.get("company_name") or "").strip().lower()
        for lead in leads
        if lead.get("company_location") and (lead.get("company_name") or "").strip()
    }
    missing_hq = [name for name in unique_companies if name.lower() not in located]
    cache: dict[str, Any] = {}
    need_domain = bool(HUNTER_API_KEY)

    with ddg_session() as ddgs:
        total_cos = max(1, len(unique_companies))
        for i, company in enumerate(unique_companies):
            if time.monotonic() > deadline:
                break
            report(91 + int((i / total_cos) * 4), f"Looking up {company}")
            skip_location = company.lower() not in {n.lower() for n in missing_hq}
            if skip_location and not need_domain:
                continue
            profile = lookup_company(
                company,
                ddgs=ddgs,
                skip_location=skip_location,
                skip_domain=not need_domain,
            )
            cache[company.lower()] = profile
            if profile:
                companies_looked_up += 1
            if profile and profile.location:
                for lead in leads:
                    if (lead.get("company_name") or "").strip().lower() == company.lower():
                        if not lead.get("company_location"):
                            lead["company_location"] = profile.location
                        if getattr(profile, "domain", ""):
                            lead["company_domain"] = profile.domain

        report(96, "Checking snippets for published emails")
        mailer = EmailEnrichmentService()
        for lead in leads:
            if time.monotonic() > deadline:
                break
            name = (lead.get("name") or "").strip()
            if not name or name == "Unknown":
                continue
            result = mailer.enrich(
                PersonInput(
                    name=name,
                    job_title=str(lead.get("work_designation") or ""),
                    company_name=str(lead.get("company_name") or ""),
                    company_domain=str(lead.get("company_domain") or ""),
                    snippet=str(lead.get("raw_snippet") or ""),
                ),
                use_providers=False,
                use_public_web=False,
                verify=True,
            )
            if result.email:
                lead["email"] = result.email
            lead["email_status"] = result.status
            lead["email_confidence"] = result.confidence
            lead["email_source"] = result.source or None
            lead["email_pattern"] = result.pattern or None
            lead["email_source_url"] = result.source_url or None
            lead["email_evidence"] = [item.to_dict() for item in result.evidence] or None
            lead["email_verification"] = result.verification.to_dict() if result.verification else None
            if result.domain:
                lead["company_domain"] = result.domain

    stats = {
        "companies": companies_looked_up,
        "emails": sum(1 for lead in leads if lead.get("email")),
        "locations": sum(1 for lead in leads if lead.get("company_location")),
        "people_locations": sum(1 for lead in leads if lead.get("personal_location")),
    }
    return leads, stats
