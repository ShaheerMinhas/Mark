from __future__ import annotations

from typing import Any

from backend.services.CompanyModule.extract import extract_location
from backend.services.EmailModule.domain import discover_company_domain
from backend.services.ddg import ddg_text


def search_company_location(company: str, ddgs: Any | None = None) -> tuple[str, str]:
    """Return (location, evidence) from public search snippets."""
    if not company:
        return "", ""
    queries = [
        f'"{company.strip()}" headquarters',
        f"{company.strip()} HQ location",
    ]
    for query in queries:
        for hit in ddg_text(query, max_results=5, ddgs=ddgs):
            blob = " ".join(
                part for part in [hit.get("title") or "", hit.get("body") or hit.get("snippet") or ""] if part
            )
            location = extract_location(blob)
            if location:
                return location, f"search: {query}"
    return "", ""


def company_website(company: str, ddgs: Any | None = None) -> tuple[str, str]:
    """Return (domain, website url)."""
    domain = discover_company_domain(company, ddgs=ddgs)
    if not domain:
        return "", ""
    return domain, f"https://{domain}"
