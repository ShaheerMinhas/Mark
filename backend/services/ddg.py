"""Shared DuckDuckGo client so enrichment does not open a new session per query."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
import time

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS  # type: ignore

_last_call = 0.0
_MIN_INTERVAL = 1.8


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


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
    if not query or not query.strip():
        return []

    def _run(client: Any) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"max_results": max_results}
        try:
            return list(client.text(query, region="us-en", page=page, **kwargs) or [])
        except TypeError:
            try:
                return list(client.text(query, page=page, **kwargs) or [])
            except TypeError:
                return list(client.text(query, **kwargs) or [])

    def _fresh() -> list[dict[str, Any]]:
        _throttle()
        try:
            with _make_client() as client:
                return _run(client)
        except Exception:
            return []

    hits: list[dict[str, Any]] = []
    if ddgs is not None:
        _throttle()
        try:
            hits = _run(ddgs)
        except Exception:
            hits = []
        if hits:
            return hits
        if not retry:
            return _fresh()

    hits = _fresh()
    if hits or not retry:
        return hits
    time.sleep(2.5)
    return _fresh()


@contextmanager
def ddg_session() -> Iterator[Any | None]:
    try:
        with _make_client() as client:
            yield client
    except Exception:
        yield None
