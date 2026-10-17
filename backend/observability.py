"""Structured logging for the discovery pipeline.

Every log line carries the SearchJob's trace id so a single search can be followed
across discovery, qualification and enrichment. Values are emitted as key=value
pairs, which greps cleanly in the Railway log viewer without adding a dependency.

Never pass API keys, tokens, passwords or full contact details through here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

_SENSITIVE = ("key", "token", "password", "secret", "cookie", "authorization")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def _is_sensitive(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SENSITIVE)


def _render(value: Any) -> str:
    text = str(value)
    if any(ch in text for ch in " \t\"="):
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    parts = [f"event={_render(event)}"]
    for name, value in fields.items():
        if value is None or _is_sensitive(name):
            continue
        parts.append(f"{name}={_render(value)}")
    logger.log(level, " ".join(parts))


def configure_logging(level: str | None = None) -> None:
    """Install a single stderr handler. Safe to call more than once."""
    root = logging.getLogger()
    resolved = (level or "INFO").upper()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))
