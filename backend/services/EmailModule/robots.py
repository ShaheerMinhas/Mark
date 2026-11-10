"""Robots.txt check for email-module page fetches. Fail closed.

The fetch uses the pinned address check. A disallow is counted. A missing or
unreadable robots.txt does not allow the page and is not counted as a disallow.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from sqlalchemy import text

from backend.services.discovery_engine.ssrf import SsrfError, pinned_opener, safe_fetch

_UA = "MarkBot/1.0"
_CACHE: dict[str, str] = {}


def clear_robots_cache() -> None:
    _CACHE.clear()


def _pinned(url: str) -> bytes:
    if os.environ.get("MARK_DISABLE_CONTACT_NETWORK") == "1":
        raise SsrfError("page fetch is disabled in this process")
    host = (urlparse(url).hostname or "").strip(".").lower()
    return safe_fetch(url, opener=pinned_opener, allowed_hosts={host}, timeout=8, max_bytes=200_000)


def note_robots_disallow(host: str) -> None:
    """Increment the persisted robots skip count. The host is not a full URL."""
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO fetch_stats (name, count) VALUES ('robots_disallow', 1) "
                "ON CONFLICT (name) DO UPDATE SET count = fetch_stats.count + 1"
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    del host


def robots_skipped_count(db) -> int:
    from backend.models import FetchStat

    row = db.get(FetchStat, "robots_disallow")
    return int(row.count) if row is not None else 0


def page_decision(url: str, *, fetch=None) -> str:
    """Return allow, disallow, unavailable, blocked_host, or refused.

    ``disallow`` is the only decision that increments the robots skip count.
    """
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").strip(".").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return "refused"
    if host == "linkedin.com" or host.endswith(".linkedin.com") or host == "facebook.com" or host.endswith(".facebook.com"):
        return "blocked_host"
    cached = _CACHE.get(host)
    if cached is None:
        robots_url = f"{parsed.scheme}://{host}/robots.txt"
        try:
            raw = (fetch or _pinned)(robots_url)
        except SsrfError:
            _CACHE[host] = ""
            return "unavailable"
        text_body = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        _CACHE[host] = text_body
        cached = text_body
    if not cached.strip():
        return "unavailable"
    parser = RobotFileParser()
    parser.parse(cached.splitlines())
    try:
        allowed = bool(parser.can_fetch(_UA, url))
    except Exception:
        return "unavailable"
    if not allowed:
        note_robots_disallow(host)
        return "disallow"
    return "allow"


def allowed(url: str) -> bool:
    return page_decision(url) == "allow"


def fetch_public_page(url: str, *, fetch=None) -> str:
    """Return page text after a robots allow. Empty when the page must not be opened."""
    if page_decision(url, fetch=fetch) != "allow":
        return ""
    getter = fetch or _pinned
    try:
        raw = getter(url)
    except SsrfError:
        return ""
    text_body = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    return text_body[:250_000]
