"""Capped exponential backoff. Jitter is an explicit 0..1 input so tests can pin it."""

from __future__ import annotations


def backoff_seconds(attempt: int, *, base: float = 5.0, cap: float = 900.0, jitter: float = 0.0) -> float:
    """Return a delay in seconds.

    ``jitter`` 0 keeps half the raw delay. ``jitter`` 1 keeps the full raw delay.
    """
    clamped = min(1.0, max(0.0, float(jitter)))
    raw = min(float(cap), float(base) * (2 ** max(0, int(attempt) - 1)))
    return raw * (0.5 + 0.5 * clamped)


class RateLimited(Exception):
    """A provider asked us to slow down. The run stays queued."""
