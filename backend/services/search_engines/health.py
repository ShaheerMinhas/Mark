"""Per-engine availability tracking.

Separate from ``services.provider_health`` on purpose: that registry reports which
*email* providers are degraded, and mixing search engines into it would make those
logs wrong. The policy is the same, the bookkeeping is not shared.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from backend.services.search_engines.base import SearchOutcome

#: Consecutive non-rate-limit errors before an engine is benched.
FAILURE_THRESHOLD = 3

#: A rate limit benches an engine immediately, for this long to start with.
RATE_LIMIT_COOLDOWN_SECONDS = 120.0

#: Plain errors (bad markup, 5xx) recover faster than throttling.
ERROR_COOLDOWN_SECONDS = 30.0

#: Repeat offences back off exponentially, up to this.
MAX_COOLDOWN_SECONDS = 900.0


@dataclass
class _State:
    failures: int = 0
    strikes: int = 0
    blocked_until: float = 0.0
    successes: int = 0
    last_outcome: str = ""


_states: dict[str, _State] = {}
_lock = threading.Lock()


def _state(engine: str) -> _State:
    if engine not in _states:
        _states[engine] = _State()
    return _states[engine]


def record_success(engine: str) -> None:
    with _lock:
        state = _state(engine)
        state.failures = 0
        state.strikes = 0
        state.blocked_until = 0.0
        state.successes += 1
        state.last_outcome = SearchOutcome.OK.value


def record_failure(engine: str, outcome: SearchOutcome) -> None:
    """Bench an engine when it is throttling us or repeatedly failing."""
    with _lock:
        state = _state(engine)
        state.last_outcome = outcome.value
        if outcome == SearchOutcome.RATE_LIMITED:
            state.strikes += 1
            cooldown = min(
                MAX_COOLDOWN_SECONDS,
                RATE_LIMIT_COOLDOWN_SECONDS * (2 ** (state.strikes - 1)),
            )
            state.blocked_until = time.monotonic() + cooldown
            state.failures = 0
            return
        state.failures += 1
        if state.failures >= FAILURE_THRESHOLD:
            state.strikes += 1
            cooldown = min(
                MAX_COOLDOWN_SECONDS, ERROR_COOLDOWN_SECONDS * (2 ** (state.strikes - 1))
            )
            state.blocked_until = time.monotonic() + cooldown
            state.failures = 0


def is_blocked(engine: str) -> bool:
    with _lock:
        return _state(engine).blocked_until > time.monotonic()


def retry_after(engine: str) -> float:
    with _lock:
        return max(0.0, _state(engine).blocked_until - time.monotonic())


def blocked_engines() -> list[str]:
    now = time.monotonic()
    with _lock:
        return sorted(n for n, s in _states.items() if s.blocked_until > now)


def snapshot() -> dict[str, dict[str, float | int | str]]:
    """Current per-engine counters, for logging and diagnostics."""
    now = time.monotonic()
    with _lock:
        return {
            name: {
                "successes": state.successes,
                "failures": state.failures,
                "strikes": state.strikes,
                "blocked_for": round(max(0.0, state.blocked_until - now), 1),
                "last_outcome": state.last_outcome,
            }
            for name, state in _states.items()
        }


def reset() -> None:
    with _lock:
        _states.clear()
