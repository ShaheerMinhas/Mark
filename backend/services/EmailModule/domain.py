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

#: Well-known university / employer hosts. Skipping DDG discovery here makes
#: `"name" @domain` email searches fire on every faculty lead instead of only
#: when the website SERP cooperates.
KNOWN_COMPANY_DOMAINS: dict[str, str] = {
    "nust": "nust.edu.pk",
    "nust islamabad": "nust.edu.pk",
    "nust smme": "nust.edu.pk",
    "nust seecs": "nust.edu.pk",
    "nust ceme": "nust.edu.pk",
    "national university of sciences and technology": "nust.edu.pk",
    "fast": "nu.edu.pk",
    "fast nuces": "nu.edu.pk",
    "nuces": "nu.edu.pk",
    "national university of computer and emerging sciences": "nu.edu.pk",
    "lums": "lums.edu.pk",
    "lahore university of management sciences": "lums.edu.pk",
    "uet": "uet.edu.pk",
    "uet lahore": "uet.edu.pk",
    "university of engineering and technology": "uet.edu.pk",
    "uet taxila": "uettaxila.edu.pk",
    "umt": "umt.edu.pk",
    "umt lahore": "umt.edu.pk",
    "university of management and technology": "umt.edu.pk",
    "ucp": "ucp.edu.pk",
    "university of central punjab": "ucp.edu.pk",
    "comsats": "comsats.edu.pk",
    "comsats university": "comsats.edu.pk",
    "comsats university islamabad": "comsats.edu.pk",
    "cui": "comsats.edu.pk",
    "uaf": "uaf.edu.pk",
    "university of agriculture faisalabad": "uaf.edu.pk",
    "gcuf": "gcuf.edu.pk",
    "government college university faisalabad": "gcuf.edu.pk",
    "punjab university": "pu.edu.pk",
    "university of the punjab": "pu.edu.pk",
    "pu lahore": "pu.edu.pk",
    "qau": "qau.edu.pk",
    "quaid-i-azam university": "qau.edu.pk",
    "quaid i azam university": "qau.edu.pk",
    "iiui": "iiu.edu.pk",
    "international islamic university": "iiu.edu.pk",
    "bahria university": "bahria.edu.pk",
    "air university": "au.edu.pk",
    "fccu": "fccollege.edu.pk",
    "forman christian college": "fccollege.edu.pk",
    "university of education": "ue.edu.pk",
    "gcu lahore": "gcu.edu.pk",
    "government college university lahore": "gcu.edu.pk",
    "pieas": "pieas.edu.pk",
    "iba": "iba.edu.pk",
    "institute of business administration": "iba.edu.pk",
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


def resolve_known_domain(company: str) -> str:
    """Return a hardcoded employer domain when the company name is a known alias."""
    key = re.sub(r"\s+", " ", (company or "").strip().lower()).strip(" ,.")
    if not key:
        return ""
    if key in KNOWN_COMPANY_DOMAINS:
        return KNOWN_COMPANY_DOMAINS[key]
    # Longest alias match inside the company string (NUST SEECS, Faculty at LUMS, …).
    for alias, host in sorted(KNOWN_COMPANY_DOMAINS.items(), key=lambda item: -len(item[0])):
        if len(alias) < 3:
            continue
        if re.search(rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)", key):
            return host
    return ""


def discover_company_domain(company: str, ddgs: Any | None = None) -> str:
    """Public search for the company's website host. Empty if unknown."""
    known = resolve_known_domain(company)
    if known:
        return known
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
