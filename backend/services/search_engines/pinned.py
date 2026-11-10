"""Interactive search through the pinned fetch.

The live client asks only html.duckduckgo.com, then search.yahoo.co.jp when that
page has no results. It does not open result links and it does not use the
ddgs or primp clients. Tests inject nothing here; callers patch ``safe_fetch``.
"""

from __future__ import annotations

import os
import re
from html import unescape
from urllib.parse import quote_plus, unquote, urlparse

from backend.services.discovery_engine.ssrf import SsrfError, pinned_opener

DDG_HOST = "html.duckduckgo.com"
YAHOO_HOST = "search.yahoo.co.jp"
_TAG = re.compile(r"<[^>]+>")
_DDG_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_DDG_SNIP = re.compile(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span|div)>', re.I | re.S)


def _disabled() -> bool:
    return os.environ.get("MARK_DISABLE_CONTACT_NETWORK") == "1"


def _fetch(url: str, host: str) -> bytes:
    if _disabled():
        raise SsrfError("search fetch is disabled in this process")
    from backend.services.discovery_engine.ssrf import safe_fetch

    return safe_fetch(url, opener=pinned_opener, allowed_hosts={host}, timeout=8, max_bytes=300_000)


def _robots_allow(host: str, path: str) -> bool:
    try:
        body = _fetch(f"https://{host}/robots.txt", host)
    except SsrfError:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    if not text.strip():
        return False
    if "disallow: /" in text and f"allow: {path}" not in text and "allow: /" not in text:
        return False
    return True


def _result_url(href: str) -> str:
    raw = unescape(href or "").strip()
    if "uddg=" in raw:
        raw = unquote(raw.split("uddg=", 1)[1].split("&", 1)[0])
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return raw


def parse_duckduckgo(page: str) -> list[dict[str, str]]:
    snippets = [re.sub(r"\s+", " ", _TAG.sub(" ", unescape(item))).strip() for item in _DDG_SNIP.findall(page or "")]
    hits = []
    for index, (href, inner) in enumerate(_DDG_LINK.findall(page or "")):
        url = _result_url(href)
        title = re.sub(r"\s+", " ", _TAG.sub(" ", unescape(inner))).strip()
        if not url or not title:
            continue
        body = snippets[index] if index < len(snippets) else ""
        hits.append({"title": title, "href": url, "body": body})
    return hits


def parse_yahoo(page: str) -> list[dict[str, str]]:
    from lxml import html

    try:
        tree = html.fromstring(page or "")
    except Exception:
        return []
    hits = []
    for card in tree.xpath("//div[contains(@class, 'sw-CardBase') and .//h3]"):
        title = re.sub(r"\s+", " ", " ".join(card.xpath(".//h3//text()"))).strip()
        hrefs = card.xpath(".//a[.//h3]/@href") or card.xpath(".//a/@href")
        href = _result_url(hrefs[0] if hrefs else "")
        if not title or not href or YAHOO_HOST in href:
            continue
        body = re.sub(r"\s+", " ", " ".join(card.xpath(".//*[contains(@class, 'sw-Card__summary')]//text()"))).strip()
        hits.append({"title": title, "href": href, "body": body})
    return hits


def pinned_search(query: str, *, max_results: int, page: int) -> list[dict[str, str]]:
    """One or two pinned SERP requests. Result URLs are returned, not fetched."""
    if _disabled():
        return []
    start = max(1, (max(page, 1) - 1) * 10 + 1)
    hits: list[dict[str, str]] = []
    if _robots_allow(DDG_HOST, "/html"):
        try:
            raw = _fetch(f"https://{DDG_HOST}/html/?q={quote_plus(query)}", DDG_HOST)
            hits = parse_duckduckgo(raw.decode("utf-8", errors="replace"))
        except SsrfError:
            hits = []
    if not hits and _robots_allow(YAHOO_HOST, "/search"):
        try:
            raw = _fetch(f"https://{YAHOO_HOST}/search?p={quote_plus(query)}&b={start}", YAHOO_HOST)
            hits = parse_yahoo(raw.decode("utf-8", errors="replace"))
        except SsrfError:
            hits = []
    return hits[:max_results]
