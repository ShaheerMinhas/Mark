"""Public-web email discovery with robots.txt respect. No LinkedIn login."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from backend.services.EmailModule.extract import email_fits_person, extract_emails
from backend.services.EmailModule.robots import allowed
from backend.services.EmailModule.web import _fetch_text, _search_queries
from backend.services.ddg import ddg_text

#: These hosts show up in email SERPs and never yield a public address.
_SKIP_HOSTS = (
    "zoominfo.com",
    "rocketreach.co",
    "linkedin.com",
    "facebook.com",
    "apollo.io",
    "lusha.com",
    "signalhire.com",
    "contactout.com",
    "beenverified.com",
)


def _skip_fetch(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return any(host == domain or host.endswith("." + domain) for domain in _SKIP_HOSTS)


def discover_public_emails(
    name: str,
    company: str = "",
    domain: str = "",
    *,
    snippet: str = "",
    snippets_only: bool = False,
    ddgs: Any | None = None,
    max_pages: int = 4,
) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(email: str, url: str, detail: str) -> None:
        key = email.lower()
        if key in seen:
            return
        if name and not email_fits_person(email, name):
            return
        seen.add(key)
        found.append({"email": email, "url": url, "detail": detail})

    for email in extract_emails(snippet, name=name, require_name_match=bool(name)):
        add(email, "", "already present in profile text")

    if snippets_only:
        return found

    queries = _search_queries(name, company, domain)

    pages = 0
    hrefs: list[str] = []
    for query in queries:
        for raw in ddg_text(query, max_results=5, ddgs=ddgs):
            title = raw.get("title") or ""
            body = raw.get("body") or raw.get("snippet") or ""
            href = (raw.get("href") or raw.get("link") or "").strip()
            for email in extract_emails(f"{title}\n{body}", name=name, require_name_match=bool(name)):
                add(email, href, f"search snippet: {query}")
            if href:
                hrefs.append(href)
        if found:
            return found

    for href in hrefs:
        if pages >= min(max_pages, 2):
            break
        if _skip_fetch(href) or not allowed(href):
            continue
        page = _fetch_text(href)
        pages += 1
        if not page:
            continue
        for email in extract_emails(page, name=name, require_name_match=bool(name)):
            add(email, href, f"public page {href}")
        if found:
            return found

    return found
