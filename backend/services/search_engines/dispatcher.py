"""Picks an engine per query and falls back when one starts throttling.

Two jobs. First, *spread*: each engine has its own throttle, so rotating across
five engines raises sustained throughput well above what one engine allows.
Second, *survive*: when an engine rate-limits, it is benched and the query is
retried on the next healthy engine instead of being abandoned.
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Any, Sequence

from backend.observability import log_event
from backend.services.search_engines import health
from backend.services.search_engines.base import (
    SearchOutcome,
    SearchResponse,
)
from backend.services.search_engines.engines import default_engines

logger = logging.getLogger(__name__)


class EngineDispatcher:
    """Round-robins queries across engines, skipping benched ones."""

    def __init__(self, engines: Sequence[Any] | None = None) -> None:
        self._engines: list[Any] = list(engines) if engines is not None else default_engines()
        self._cycle = itertools.cycle(range(len(self._engines))) if self._engines else None
        self._lock = threading.Lock()

    @property
    def engines(self) -> list[Any]:
        return list(self._engines)

    def engine_names(self) -> list[str]:
        return [engine.name for engine in self._engines]

    def available(self) -> list[Any]:
        return [e for e in self._engines if not health.is_blocked(e.name)]

    def _ordered_for_next_query(self) -> list[Any]:
        """Healthy engines, rotated so consecutive queries use different ones."""
        if not self._engines:
            return []
        with self._lock:
            start = next(self._cycle) if self._cycle else 0
        count = len(self._engines)
        rotated = [self._engines[(start + offset) % count] for offset in range(count)]
        healthy = [e for e in rotated if not health.is_blocked(e.name)]
        # All benched: fall back to the one that will free up soonest rather than
        # reporting a failure we have not actually observed.
        if not healthy:
            return [min(rotated, key=lambda e: health.retry_after(e.name))]
        return healthy

    def search(
        self,
        query: str,
        *,
        max_results: int = 12,
        page: int = 1,
        max_attempts: int | None = None,
    ) -> SearchResponse:
        """Try healthy engines in rotation until one answers. Never raises.

        ``max_attempts`` caps how many engines a single query may touch. Email
        lookups pass 2 so a miss does not waterfall through the whole replica
        set and stall the batch.
        """
        if not query or not query.strip():
            return SearchResponse(SearchOutcome.EMPTY, detail="blank query")

        candidates = self._ordered_for_next_query()
        if not candidates:
            return SearchResponse(SearchOutcome.ERROR, detail="no_engines_configured")

        last = SearchResponse(SearchOutcome.ERROR, detail="no_engine_attempted")
        empty: SearchResponse | None = None
        attempts = 0

        for engine in candidates:
            response = engine.fetch(query, max_results=max_results, page=page)
            last = response
            attempts += 1

            if response.outcome == SearchOutcome.OK:
                health.record_success(engine.name)
                return response

            if response.outcome == SearchOutcome.EMPTY:
                # The engine answered, so it is healthy, but one engine having no
                # results does not mean the others have none. Keep looking and only
                # report empty if every engine agrees.
                health.record_success(engine.name)
                empty = empty or response
            else:
                health.record_failure(engine.name, response.outcome)
                log_event(
                    logger,
                    "engine.failed",
                    level=logging.WARNING,
                    engine=engine.name,
                    outcome=response.outcome.value,
                    detail=response.detail,
                    blocked_for=round(health.retry_after(engine.name), 1) or None,
                )

            if max_attempts is not None and attempts >= max_attempts:
                return empty or last

        return empty or last


#: Module-level dispatcher. Tests build their own with stub engines.
dispatcher = EngineDispatcher()
#: Separate clients for email/company lookups so enrichment does not sit behind
#: discovery's per-engine throttle.
email_dispatcher = EngineDispatcher(default_engines(pool="email"))


def search_text(
    query: str,
    *,
    max_results: int = 12,
    page: int = 1,
    ddgs: Any | None = None,
) -> SearchResponse:
    """Run one query across the engine pool.

    ``ddgs`` is accepted for compatibility with the previous single-engine helper
    and is ignored: engines manage their own clients.
    """
    return dispatcher.search(query, max_results=max_results, page=page)


def email_search_text(
    query: str,
    *,
    max_results: int = 8,
    page: int = 1,
    ddgs: Any | None = None,
) -> SearchResponse:
    """Email harvest: independent pool, stop after two engines so a miss is cheap."""
    return email_dispatcher.search(
        query, max_results=max_results, page=page, max_attempts=2
    )


def engine_health() -> dict[str, dict[str, float | int | str]]:
    return health.snapshot()
