from __future__ import annotations

from typing import Any

from backend.services.EmailModule.domain import discover_company_domain
from backend.services.EmailModule.extract import extract_emails
from backend.services.EmailModule.hunter import hunter_find
from backend.services.EmailModule.types import EmailHit
from backend.services.EmailModule.web import web_find


def find_emails(
    name: str,
    *,
    company: str = "",
    snippet: str = "",
    domain: str = "",
    all_sources: bool = False,
    max_results: int = 5,
    quick: bool = False,
    ddgs: Any | None = None,
) -> list[EmailHit]:
    """Waterfall lookup. Stops at the first real hit unless ``all_sources`` is set."""
    hits: list[EmailHit] = []
    seen: set[str] = set()

    def add(item: EmailHit | None) -> None:
        if not item or not item.email:
            return
        key = item.email.lower()
        if key in seen:
            return
        seen.add(key)
        hits.append(item)

    for email in extract_emails(snippet, name=name, require_name_match=bool(name)):
        add(
            EmailHit(
                email=email,
                source="snippet",
                confidence="found",
                evidence="already present in profile text",
                domain=email.rsplit("@", 1)[-1],
            )
        )
    if hits and not all_sources:
        return hits[:max_results]

    resolved = (domain or "").strip().lower().removeprefix("www.")
    if not resolved and company and not quick:
        resolved = discover_company_domain(company, ddgs=ddgs)

    hunter_hit = hunter_find(name, resolved)
    add(hunter_hit)
    if hits and not all_sources:
        return hits[:max_results]

    for hit in web_find(
        name,
        company=company,
        domain=resolved,
        max_hits=max_results,
        snippets_only=quick,
        ddgs=ddgs,
    ):
        add(hit)
        if hits and not all_sources:
            return hits[:max_results]

    return hits[:max_results]


def find_email(
    name: str,
    *,
    company: str = "",
    snippet: str = "",
    domain: str = "",
    quick: bool = False,
    ddgs: Any | None = None,
) -> EmailHit | None:
    found = find_emails(
        name,
        company=company,
        snippet=snippet,
        domain=domain,
        all_sources=False,
        max_results=1,
        quick=quick,
        ddgs=ddgs,
    )
    return found[0] if found else None
