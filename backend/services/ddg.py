"""Shared DuckDuckGo client so enrichment does not open a new session per query.

Discovery now searches through ``services.search_engines``, which rotates across
several engines. This module remains the DuckDuckGo client used by enrichment and
company lookup, and re-exports the shared result types for existing callers.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator
import time

from backend.services.search_engines.base import (
    SearchOutcome,
    SearchResponse,
    classify_exception as _classify,
)

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS  # type: ignore

__all__ = [
    "SearchOutcome",
    "SearchResponse",
    "ddg_session",
    "ddg_text",
    "search_text",
]

_last_call = 0.0
_MIN_INTERVAL = 1.8
#: The throttle is process-wide, so concurrent searches must not race on it.
_throttle_lock = threading.Lock()


def _throttle() -> None:
    global _last_call
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def search_text(
    query: str,
    *,
    max_results: int = 12,
    page: int = 1,
    ddgs: Any | None = None,
) -> SearchResponse:
    """Run one query and report what actually happened. Never raises."""
    if not query or not query.strip():
        return SearchResponse(SearchOutcome.EMPTY, detail="blank query")

    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    _throttle()
    try:
        if ddgs is not None:
            hits = _run(ddgs, query, max_results, page)
        else:
            with _make_client() as client:
                hits = _run(client, query, max_results, page)
    except Exception as exc:
        outcome, detail = _classify(exc)
        return SearchResponse(outcome, detail=detail, duration_ms=_elapsed())

    if not hits:
        return SearchResponse(SearchOutcome.EMPTY, duration_ms=_elapsed())
    return SearchResponse(SearchOutcome.OK, hits=hits, duration_ms=_elapsed())


def _run(client: Any, query: str, max_results: int, page: int) -> list[dict[str, Any]]:
    try:
        return list(client.text(query, region="us-en", page=page, max_results=max_results) or [])
    except TypeError:
        try:
            return list(client.text(query, page=page, max_results=max_results) or [])
        except TypeError:
            return list(client.text(query, max_results=max_results) or [])


def _make_client() -> Any:
    try:
        return DDGS(timeout=10)
    except TypeError:
        return DDGS()


def ddg_text(
    query: str,
    max_results: int = 6,
    ddgs: Any | None = None,
    *,
    page: int = 1,
    retry: bool = True,
) -> list[dict[str, Any]]:
    """Return hits as a plain list.

    Discovery uses the multi-engine dispatcher. This helper is for enrichment
    and company lookup: it shares that pool so a single email lookup does not
    waterfall through every ``ddgs`` backend and starve discovery.
    """
    if not query or not query.strip():
        return []

    if ddgs is not None:
        _throttle()
        try:
            try:
                hits = list(
                    ddgs.text(
                        query,
                        backend="duckduckgo",
                        max_results=max_results,
                        page=page,
                    )
                    or []
                )
            except TypeError:
                hits = list(ddgs.text(query, max_results=max_results) or [])
        except Exception:
            hits = []
        if hits or not retry:
            return hits

    from backend.services.search_engines.dispatcher import email_search_text as pooled

    return pooled(query, max_results=max_results, page=page).hits


@contextmanager
def ddg_session() -> Iterator[Any | None]:
    try:
        with _make_client() as client:
            yield client
    except Exception:
        yield None
