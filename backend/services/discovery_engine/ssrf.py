"""Reject destinations a global job must not fetch. DNS resolution is intentionally not done here."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


def public_http_url(value: str | None) -> str | None:
    """Return the URL when it is http(s) and not a local or link-local target. Otherwise None."""
    if not value:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.strip(".").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return None
    return parsed.geturl()
