"""Multi-engine public search.

Discovery used to run every query through DuckDuckGo alone, which throttled about
a third of them away. Queries now rotate across several free engines, each with
its own pacing and its own health state.
"""

from __future__ import annotations

from backend.services.search_engines.base import (
    SearchOutcome,
    SearchResponse,
    Throttle,
)
from backend.services.search_engines.dispatcher import (
    EngineDispatcher,
    dispatcher,
    email_search_text,
    engine_health,
    search_text,
)

__all__ = [
    "EngineDispatcher",
    "SearchOutcome",
    "SearchResponse",
    "Throttle",
    "dispatcher",
    "email_search_text",
    "engine_health",
    "search_text",
]
