"""Minimal robots.txt check. Fail closed on fetch errors for constructed paths."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

_CACHE: dict[str, RobotFileParser | None] = {}
_UA = "MarkBot/1.0 (lead enrichment; public pages only)"


def robots_for(url: str) -> RobotFileParser | None:
    host = urlparse(url).netloc.lower()
    if not host:
        return None
    if host in _CACHE:
        return _CACHE[host]
    robots_url = urljoin(f"{urlparse(url).scheme or 'https'}://{host}", "/robots.txt")
    parser = RobotFileParser()
    try:
        resp = httpx.get(robots_url, timeout=6.0, headers={"User-Agent": _UA}, follow_redirects=True)
        if resp.status_code >= 400:
            _CACHE[host] = None
            return None
        parser.parse(resp.text.splitlines())
        _CACHE[host] = parser
        return parser
    except Exception:
        _CACHE[host] = None
        return None


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if not host:
        return False
    if "linkedin.com" in host or "facebook.com" in host:
        return False
    parser = robots_for(url)
    if parser is None:
        return True
    try:
        return bool(parser.can_fetch(_UA, url))
    except Exception:
        return False
