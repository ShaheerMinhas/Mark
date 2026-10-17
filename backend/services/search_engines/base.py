"""Shared types and helpers for public search engines.

The outcome/response types live here rather than in ``ddg`` because discovery is
no longer tied to a single engine; ``backend.services.ddg`` re-exports them so
existing imports keep working.
"""

from __future__ import annotations

import html as html_module
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Protocol
from urllib.parse import parse_qs, unquote, urlparse


class SearchOutcome(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class SearchResponse:
    """A single query attempt, with the failure kind preserved.

    A plain list of hits flattens every failure to "no results", which makes a
    rate limit indistinguishable from a genuine zero-result query. Discovery needs
    the difference to decide between `completed` and `partially_completed`.
    """

    outcome: SearchOutcome
    hits: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    duration_ms: int = 0
    #: Which engine produced this, for logging and health accounting.
    engine: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (SearchOutcome.OK, SearchOutcome.EMPTY)

    @property
    def retryable(self) -> bool:
        return self.outcome in (SearchOutcome.RATE_LIMITED, SearchOutcome.TIMEOUT)


def classify_exception(exc: Exception) -> tuple[SearchOutcome, str]:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "ratelimit" in name or "429" in text or "rate limit" in text or "too many" in text:
        return SearchOutcome.RATE_LIMITED, type(exc).__name__
    if "timeout" in name or "timeout" in text:
        return SearchOutcome.TIMEOUT, type(exc).__name__
    return SearchOutcome.ERROR, type(exc).__name__


def classify_status(status: int) -> tuple[SearchOutcome, str]:
    """Map an HTTP status from a scraped engine onto an outcome."""
    if status == 429:
        return SearchOutcome.RATE_LIMITED, "http_429"
    # Engines commonly answer a blocked scrape with 403 rather than 429.
    if status in (401, 403):
        return SearchOutcome.RATE_LIMITED, f"http_{status}"
    if status >= 500:
        return SearchOutcome.ERROR, f"http_{status}"
    if status >= 400:
        return SearchOutcome.ERROR, f"http_{status}"
    return SearchOutcome.OK, ""


class Throttle:
    """Per-engine minimum spacing between requests.

    Each engine gets its own instance. That is what makes several engines faster
    than one: a single shared throttle serialised every query behind one delay.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            pending = self.min_interval - (time.monotonic() - self._last)
            if pending > 0:
                time.sleep(pending)
            self._last = time.monotonic()


class SearchEngine(Protocol):
    name: str
    throttle: Throttle

    def fetch(self, query: str, *, max_results: int, page: int) -> SearchResponse: ...


# --- HTML scraping helpers -------------------------------------------------
# The project scrapes with plain regex elsewhere (see ``linkedin_session``), so
# these avoid pulling in an HTML parser dependency.

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_tags(fragment: str) -> str:
    return _WS.sub(" ", html_module.unescape(_TAGS.sub(" ", fragment or ""))).strip()


def clean_redirect(url: str) -> str:
    """Unwrap the redirect wrappers engines put around result links."""
    raw = html_module.unescape((url or "").strip())
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    # DuckDuckGo: /l/?uddg=<encoded>; Bing: /ck/a?...&u=<base64>; Brave: direct.
    if parsed.path.startswith("/l/") or parsed.path == "/l":
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    for key in ("uddg", "url", "u", "q"):
        values = parse_qs(parsed.query).get(key, [])
        if values and values[0].startswith("http"):
            return unquote(values[0])
    return raw if raw.startswith("http") else ""


def dedupe_hits(hits: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated URLs while preserving engine ranking order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        href = (hit.get("href") or "").split("#", 1)[0].rstrip("/").lower()
        if not href or href in seen:
            continue
        seen.add(href)
        out.append(hit)
    return out
