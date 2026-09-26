from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from backend.services.ddg import ddg_text

_SKIP_HOSTS = {
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "crunchbase.com",
    "wikipedia.org",
    "glassdoor.com",
    "indeed.com",
    "zoominfo.com",
    "bloomberg.com",
    "reddit.com",
}


def host_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        return host
    except Exception:
        return ""


def company_slug(company: str) -> str:
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    skip = {"inc", "llc", "ltd", "co", "corp", "corporation", "the", "and", "company", "group"}
    kept = [w for w in words if w not in skip]
    return kept[0] if kept else ""


def discover_company_domain(company: str, ddgs: Any | None = None) -> str:
    """Public search for the company's website host. Empty if unknown."""
    if not company or not company.strip():
        return ""
    query = f"{company.strip()} official website"
    for hit in ddg_text(query, max_results=8, ddgs=ddgs):
        url = (hit.get("href") or hit.get("link") or "").strip()
        host = host_from_url(url)
        if not host:
            continue
        if any(host == skip or host.endswith("." + skip) for skip in _SKIP_HOSTS):
            continue
        if host.endswith(".pdf"):
            continue
        return host
    return ""
