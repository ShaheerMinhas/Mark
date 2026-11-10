"""Sign-in failure limit. The response does not say whether the account exists."""

from __future__ import annotations

import time
from threading import Lock

WINDOW_SECONDS = 600
MAX_FAILURES = 8

_lock = Lock()
_failures: dict[str, list[float]] = {}


def too_many_attempts(bucket: str) -> bool:
    now = time.monotonic()
    with _lock:
        recent = [stamp for stamp in _failures.get(bucket, []) if now - stamp < WINDOW_SECONDS]
        _failures[bucket] = recent
        return len(recent) >= MAX_FAILURES


def record_failure(bucket: str) -> None:
    now = time.monotonic()
    with _lock:
        recent = [stamp for stamp in _failures.get(bucket, []) if now - stamp < WINDOW_SECONDS]
        recent.append(now)
        _failures[bucket] = recent


def reset_attempts() -> None:
    with _lock:
        _failures.clear()
