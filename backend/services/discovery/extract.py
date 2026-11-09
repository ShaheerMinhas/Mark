"""Turn raw SERP hits into candidate records.

Extraction does no filtering — every parseable person becomes a candidate so the
raw pool stays visible. Qualification decides relevance separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.lead_finder import (
    _company_from_snippet,
    _location_from_snippet,
    _looks_like_role,
    _parse_linkedin_title,
    _role_from_snippet,
)
from backend.services.identity import identity_key, normalize_linkedin_url


@dataclass
class ExtractedCandidate:
    identity_key: str
    name: str
    title: str
    company: str
    location: str
    linkedin_url: str
    source_url: str
    raw_snippet: str
    #: Kept for qualification, which matches against the full SERP title.
    serp_title: str = field(default="")


def extract_candidate(hit: dict[str, Any]) -> ExtractedCandidate | None:
    """Parse one SERP hit, or ``None`` when it is not an identifiable person."""
    url = (hit.get("href") or hit.get("link") or "").strip()
    normalized = normalize_linkedin_url(url)
    if not normalized:
        return None

    serp_title = hit.get("title") or ""
    body = hit.get("body") or hit.get("snippet") or ""
    name, role, company = _parse_linkedin_title(serp_title, body)
    if not name:
        return None
    # Title parsers occasionally stash the job title in ``company`` when the SERP
    # line is only "Name - Associate Professor". Recover the real employer from
    # the snippet instead of keeping a role string as the company.
    if company and _looks_like_role(company) and not role:
        role, company = company, ""
    if not role:
        role = _role_from_snippet(body)
    if not company:
        # The SERP title often carries only "Name - Job Title"; the employer is in
        # the snippet body. Without this the candidate has no company at all.
        company = _company_from_snippet(body, name=name)

    key = identity_key(linkedin_url=normalized, name=name, company=company, title=role)
    if not key:
        return None

    return ExtractedCandidate(
        identity_key=key,
        name=name,
        title=role,
        company=company,
        location=_location_from_snippet(body),
        linkedin_url=normalized,
        source_url=url,
        raw_snippet=(body or serp_title)[:2000],
        serp_title=serp_title,
    )


def extract_candidates(hits: list[dict[str, Any]]) -> list[ExtractedCandidate]:
    out: list[ExtractedCandidate] = []
    for hit in hits or []:
        candidate = extract_candidate(hit)
        if candidate is not None:
            out.append(candidate)
    return out
