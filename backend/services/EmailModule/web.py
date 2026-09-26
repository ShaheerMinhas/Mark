from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from backend.services.EmailModule.domain import host_from_url
from backend.services.EmailModule.extract import email_fits_person, extract_emails
from backend.services.EmailModule.types import EmailHit
from backend.services.ddg import ddg_text

_HEADERS = {
    "User-Agent": "MarkEmailModule/1.0 (public contact lookup)",
    "Accept": "text/html,application/xhtml+xml,text/plain",
}
_MAILTO_RE = re.compile(r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", re.I)
_MAX_PAGE_BYTES = 250_000


def _search_queries(name: str, company: str, domain: str) -> list[str]:
    queries: list[str] = []
    if name and company:
        queries.append(f'"{name}" "{company}" email')
        queries.append(f'"{name}" "{company}" contact')
    if name and domain:
        queries.append(f'"{name}" "@{domain}"')
        queries.append(f'site:{domain} "{name}" email')
        queries.append(f'site:{domain} "{name}"')
    if company and not name:
        queries.append(f'"{company}" contact email')
    return list(dict.fromkeys(queries))


def _fetch_text(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    try:
        with httpx.Client(follow_redirects=True, timeout=8.0, headers=_HEADERS) as client:
            resp = client.get(url)
        if resp.status_code >= 400:
            return ""
        content_type = (resp.headers.get("content-type") or "").lower()
        if "html" not in content_type and "text" not in content_type and "json" not in content_type:
            return ""
        return resp.text[:_MAX_PAGE_BYTES]
    except Exception:
        return ""


def _hits_from_text(text: str, *, name: str, source: str, evidence: str, domain: str = "") -> list[EmailHit]:
    out: list[EmailHit] = []
    seen: set[str] = set()
    for raw in _MAILTO_RE.findall(text) + extract_emails(text, name=name, require_name_match=False):
        email = raw.strip()
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        if name and not email_fits_person(email, name):
            continue
        out.append(
            EmailHit(
                email=email,
                source=source,
                confidence="found" if (not name or email_fits_person(email, name)) else "likely",
                evidence=evidence[:180],
                domain=email.rsplit("@", 1)[-1],
            )
        )
    return out


def web_find(
    name: str,
    company: str = "",
    domain: str = "",
    *,
    max_hits: int = 5,
    snippets_only: bool = False,
    ddgs: Any | None = None,
) -> list[EmailHit]:
    """Find published emails via public search results and linked pages."""
    queries = _search_queries(name, company, domain)
    if snippets_only:
        queries = queries[:1]
    if not queries:
        return []

    results: list[EmailHit] = []
    seen_email: set[str] = set()
    fetched: set[str] = set()

    def add(hits: list[EmailHit]) -> None:
        for hit in hits:
            key = hit.email.lower()
            if key in seen_email:
                continue
            seen_email.add(key)
            results.append(hit)

    try:
        for query in queries:
            for raw in ddg_text(query, max_results=4 if snippets_only else 6, ddgs=ddgs):
                title = raw.get("title") or ""
                body = raw.get("body") or raw.get("snippet") or ""
                href = (raw.get("href") or raw.get("link") or "").strip()
                add(
                    _hits_from_text(
                        f"{title}\n{body}",
                        name=name,
                        source="web",
                        evidence=f"search: {query}",
                        domain=domain,
                    )
                )
                if snippets_only:
                    if len(results) >= max_hits:
                        return results[:max_hits]
                    continue
                if href and href not in fetched and len(fetched) < 6:
                    fetched.add(href)
                    page = _fetch_text(href)
                    if page:
                        host = host_from_url(href) or href
                        add(
                            _hits_from_text(
                                page,
                                name=name,
                                source="page",
                                evidence=f"page {host}",
                                domain=domain,
                            )
                        )
                if len(results) >= max_hits:
                    return results[:max_hits]
    except Exception:
        return results[:max_hits]

    if snippets_only or not (domain and name) or len(results) >= max_hits:
        return results[:max_hits]

    for path in ("/contact", "/contact-us", "/about", "/team", "/about-us"):
        url = urljoin(f"https://{domain}", path)
        if url in fetched:
            continue
        fetched.add(url)
        page = _fetch_text(url)
        if page:
            add(
                _hits_from_text(
                    page,
                    name=name,
                    source="page",
                    evidence=f"page {domain}{path}",
                    domain=domain,
                )
            )
        if len(results) >= max_hits:
            break

    return results[:max_hits]
