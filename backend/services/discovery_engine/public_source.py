"""One public LinkedIn query per autonomous run, through the pinned fetch guard.

Enabled search engines share that query. DuckDuckGo HTML stays available as the
last resort. This path does not open the result links, does not log into a site,
and does not turn a snippet into an email or a phone number. A profile is stored
only when the existing LinkedIn title parser can read a name. Anything else stays null.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote_plus, unquote, urlparse

from backend.services.discovery.extract import extract_candidate

SEARCH_HOST = "html.duckduckgo.com"
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r'href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


def query_variants(target) -> list[str]:
    criteria = dict(getattr(target, "criteria", None) or {})
    countries = list((criteria.get("geography") or {}).get("include_countries") or []) or [""]
    titles = list((criteria.get("titles") or {}).get("exact") or [])
    titles += list((criteria.get("titles") or {}).get("synonyms") or [])
    if not titles:
        titles = ["cto"]
    technologies = list((criteria.get("technologies") or {}).get("include") or []) or [""]
    industries = list((criteria.get("industry") or {}).get("include") or []) or [""]
    found: list[str] = []
    for country in countries:
        for role in titles:
            for tech in technologies:
                found.append(" ".join(part for part in (f'site:linkedin.com/in "{role}"', tech, country) if part))
            for industry in industries:
                text = " ".join(part for part in (f'site:linkedin.com/in "{role}"', industry, country) if part)
                if text not in found:
                    found.append(text)
    deduped = []
    for item in found:
        if item not in deduped:
            deduped.append(item)
    return deduped or ['site:linkedin.com/in "cto"']


def next_query(target) -> str:
    choices = query_variants(target)
    cursor = int(getattr(target, "rotation_cursor", 0) or 0)
    return choices[cursor % len(choices)]


def advance_cursor(target) -> None:
    choices = query_variants(target)
    target.rotation_cursor = int(getattr(target, "rotation_cursor", 0) or 0) + 1
    if target.rotation_cursor >= len(choices):
        target.rotation_cursor = 0


def _linkedin_url(href: str) -> str | None:
    raw = unescape(href or "").strip()
    if "uddg=" in raw:
        raw = unquote(raw.split("uddg=", 1)[1].split("&", 1)[0])
    if "linkedin.com/in/" not in raw.lower():
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return None
    return raw.split("?", 1)[0]


def person_from_candidate(parsed, *, backend: str, source_url: str) -> dict:
    """One stored person. Name, title, company, and country come from the shared parser."""
    from backend.services.global_cache.normalize import country_code

    return {
        "full_name": parsed.name,
        "title": parsed.title or None,
        "company_name": parsed.company or None,
        "country_code": country_code(parsed.location),
        "profile_url": parsed.linkedin_url,
        "email": None,
        "email_status": None,
        "phone": None,
        "phone_evidenced": False,
        "source_url": source_url,
        "provider": "public_serp",
        "backends": [backend],
    }


def robots_page_allows(body: bytes | str) -> bool:
    text = body.decode("utf-8", errors="replace").lower() if isinstance(body, bytes) else str(body or "").lower()
    if "disallow: /" in text and "allow: /html" not in text:
        return False
    return True


def hits_from_html(page: str, *, query: str) -> list[dict]:
    hits = []
    source_url = f"https://{SEARCH_HOST}/html/?q={quote_plus(query)}"
    for href, inner in _LINK.findall(page or ""):
        url = _linkedin_url(href)
        if not url:
            continue
        title = _TAG.sub(" ", unescape(inner))
        title = " ".join(title.split())
        parsed = extract_candidate({"href": url, "title": title, "body": ""})
        if parsed is None or not parsed.name:
            continue
        hits.append(person_from_candidate(parsed, backend="duckduckgo", source_url=source_url))
    return hits


def search_public_profiles(target, *, fetcher=None, db=None, transport=None) -> tuple[list[dict], str]:
    """Run the target's next query on every enabled search backend.

    ``fetcher`` replaces the DuckDuckGo fetch in tests. ``transport`` replaces
    every backend's HTTP call. A backend that is blocked does not fail the others.
    """
    from backend.services.discovery_engine.search_backends import run_linkedin_search

    return run_linkedin_search(target, fetcher=fetcher, db=db, transport=transport)
