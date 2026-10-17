"""Provider circuit breaker and backoff.

The previous waterfall tried every configured provider on every lead and treated a
429 as "skip to the next one", so a rate-limited provider kept being called for
each of hundreds of leads. Here a provider that returns 429 or repeated 5xx is
tripped open for a cooldown window and skipped entirely until it closes again.

State is per-process and in-memory on purpose: it is a short-lived traffic control,
while durable per-call history already lands in the ``provider_usage`` table.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from backend.observability import log_event

logger = logging.getLogger(__name__)

#: Consecutive failures before a provider is tripped open.
FAILURE_THRESHOLD = 3

#: Cooldown after a 429, and the base for exponential backoff on repeat offences.
RATE_LIMIT_COOLDOWN_SECONDS = 900.0
SERVER_ERROR_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 3600.0


@dataclass
class _State:
    failures: int = 0
    trips: int = 0
    open_until: float = 0.0
    last_status: int | None = None


_lock = threading.Lock()
_states: dict[str, _State] = {}


def _state(provider: str) -> _State:
    if provider not in _states:
        _states[provider] = _State()
    return _states[provider]


def _cooldown(base: float, trips: int) -> float:
    return min(MAX_COOLDOWN_SECONDS, base * (2 ** max(0, trips - 1)))


def record_success(provider: str) -> None:
    with _lock:
        state = _state(provider)
        state.failures = 0
        state.trips = 0
        state.open_until = 0.0


def record_failure(provider: str, *, http_status: int | None = None, detail: str = "") -> None:
    """Record a failed call and trip the circuit when warranted."""
    with _lock:
        state = _state(provider)
        state.last_status = http_status
        state.failures += 1

        if http_status == 429:
            state.trips += 1
            cooldown = _cooldown(RATE_LIMIT_COOLDOWN_SECONDS, state.trips)
        elif http_status is not None and http_status >= 500:
            if state.failures < FAILURE_THRESHOLD:
                return
            state.trips += 1
            cooldown = _cooldown(SERVER_ERROR_COOLDOWN_SECONDS, state.trips)
        else:
            if state.failures < FAILURE_THRESHOLD:
                return
            state.trips += 1
            cooldown = _cooldown(SERVER_ERROR_COOLDOWN_SECONDS, state.trips)

        state.open_until = time.monotonic() + cooldown
        state.failures = 0

    log_event(
        logger,
        "provider.circuit_open",
        level=logging.WARNING,
        provider=provider,
        http_status=http_status,
        cooldown_seconds=int(cooldown),
        detail=(detail or "")[:120],
    )


def is_open(provider: str) -> bool:
    """True when the provider is in cooldown and should be skipped."""
    with _lock:
        state = _states.get(provider)
        if state is None or state.open_until <= 0.0:
            return False
        if time.monotonic() >= state.open_until:
            state.open_until = 0.0
            state.failures = 0
            return False
        return True


def retry_after(provider: str) -> float:
    with _lock:
        state = _states.get(provider)
        if state is None or state.open_until <= 0.0:
            return 0.0
        return max(0.0, state.open_until - time.monotonic())


def open_providers() -> list[str]:
    return sorted(name for name in list(_states) if is_open(name))


def reset(provider: str | None = None) -> None:
    """Clear circuit state. Used by tests."""
    with _lock:
        if provider is None:
            _states.clear()
        else:
            _states.pop(provider, None)


def looks_rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "429" in text or "ratelimit" in text or "rate limit" in text or "too many" in text


@dataclass
class ProviderCircuit:
    """Instance-level façade, so callers can be handed a stub in tests."""

    names: list[str] = field(default_factory=list)

    def is_open(self, provider: str) -> bool:
        return is_open(provider)

    def record_success(self, provider: str) -> None:
        record_success(provider)

    def record_failure(
        self, provider: str, *, http_status: int | None = None, detail: str = ""
    ) -> None:
        record_failure(provider, http_status=http_status, detail=detail)

    def open_providers(self) -> list[str]:
        return open_providers()

    def any_available(self) -> bool:
        """False only when every provider we have seen is currently tripped open."""
        seen = list(_states)
        if not seen:
            return True
        return any(not is_open(name) for name in seen)

    def looks_rate_limited(self, exc: BaseException) -> bool:
        return looks_rate_limited(exc)
