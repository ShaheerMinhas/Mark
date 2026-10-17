"""Public-web phone discovery. SERP snippets first, then a short page fetch."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from backend.services.EmailModule.robots import allowed
from backend.services.EmailModule.web import _fetch_text
from backend.services.PhoneModule.extract import extract_phones
from backend.services.ddg import ddg_text

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
    "whitepages.com",
    "truepeoplesearch.com",
)


def _skip_fetch(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return any(host == domain or host.endswith("." + domain) for domain in _SKIP_HOSTS)


def _search_queries(name: str, company: str) -> list[str]:
    queries: list[str] = []
    if name and company:
        queries.append(f'"{name}" "{company}" phone')
        queries.append(f'"{name}" "{company}" mobile OR tel')
    if name:
        queries.append(f'"{name}" phone OR mobile')
    return list(dict.fromkeys(queries))[:2]


def discover_public_phones(
    name: str,
    company: str = "",
    *,
    snippet: str = "",
    snippets_only: bool = False,
    ddgs: Any | None = None,
    max_pages: int = 1,
) -> list[dict[str, str]]:
    """Return ``{phone, url, detail}`` hits. Never invents numbers."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(phone: str, url: str, detail: str) -> None:
        key = "".join(ch for ch in phone if ch.isdigit())
        if key in seen:
            return
        seen.add(key)
        found.append({"phone": phone, "url": url, "detail": detail})

    for phone in extract_phones(snippet):
        add(phone, "", "already present in profile text")

    if snippets_only or found:
        return found

    hrefs: list[str] = []
    for query in _search_queries(name, company):
        for raw in ddg_text(query, max_results=5, ddgs=ddgs):
            title = raw.get("title") or ""
            body = raw.get("body") or raw.get("snippet") or ""
            href = (raw.get("href") or raw.get("link") or "").strip()
            for phone in extract_phones(f"{title}\n{body}"):
                add(phone, href, f"search snippet: {query}")
            if href:
                hrefs.append(href)
        if found:
            return found

    pages = 0
    for href in hrefs:
        if pages >= max_pages:
            break
        if _skip_fetch(href) or not allowed(href):
            continue
        page = _fetch_text(href)
        pages += 1
        if not page:
            continue
        for phone in extract_phones(page):
            add(phone, href, f"public page {href}")
        if found:
            return found

    return found
