"""One public-search query per autonomous run, through the pinned fetch guard.

The only host this path requests is the HTML search page. It does not open the
result links, does not log into a site, and does not turn a snippet into an
email or a phone number. A profile is stored only when the existing LinkedIn
title parser can read a name. Anything else stays null.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote_plus, unquote, urlparse

from backend.services.discovery.extract import extract_candidate
from backend.services.discovery_engine.ssrf import SsrfError, pinned_opener, safe_fetch

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


def hits_from_html(page: str, *, query: str) -> list[dict]:
    hits = []
    for href, inner in _LINK.findall(page or ""):
        url = _linkedin_url(href)
        if not url:
            continue
        title = _TAG.sub(" ", unescape(inner))
        title = " ".join(title.split())
        parsed = extract_candidate({"href": url, "title": title, "body": ""})
        if parsed is None or not parsed.name:
            continue
        hits.append(
            {
                "full_name": parsed.name,
                "title": parsed.title or None,
                "company_name": parsed.company or None,
                "profile_url": parsed.linkedin_url,
                "email": None,
                "email_status": None,
                "phone": None,
                "phone_evidenced": False,
                "source_url": f"https://{SEARCH_HOST}/html/?q={quote_plus(query)}",
                "provider": "public_serp",
            }
        )
    return hits


def _robots_allow(fetcher) -> bool:
    try:
        body = fetcher(f"https://{SEARCH_HOST}/robots.txt")
    except SsrfError:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    if "disallow: /" in text and "allow: /html" not in text:
        return False
    return True


def search_public_profiles(target, *, fetcher=None) -> tuple[list[dict], str]:
    """Run the target's next query. ``fetcher`` replaces the network in tests."""
    if not getattr(target, "autonomous", False):
        return [], "This target is not set to run on its own. No request was sent."
    query = next_query(target)
    target.last_query = query

    def _default(url: str) -> bytes:
        return safe_fetch(
            url,
            opener=pinned_opener,
            allowed_hosts={SEARCH_HOST},
            timeout=8,
            max_bytes=300_000,
        )

    fetch = fetcher or _default
    if not _robots_allow(fetch):
        advance_cursor(target)
        return [], f"robots.txt did not allow a fetch of {SEARCH_HOST}. No profiles were saved."
    try:
        page = fetch(f"https://{SEARCH_HOST}/html/?q={quote_plus(query)}")
    except SsrfError as exc:
        advance_cursor(target)
        return [], f"The public search fetch was refused ({exc}). No profiles were saved."
    html = page.decode("utf-8", errors="replace") if isinstance(page, bytes) else str(page)
    hits = hits_from_html(html, query=query)
    advance_cursor(target)
    if not hits:
        return [], f"No identifiable public profile was parsed for {query}. Nothing was invented."
    return hits, query
