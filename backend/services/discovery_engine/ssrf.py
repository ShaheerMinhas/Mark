"""Outbound URL policy for new discovery fetches.

Legacy search-engine and public-email clients do not use this module. Provider
APIs are fixed vendor hosts, not operator-supplied URLs. A new fetch must call
``safe_fetch`` so every address and every redirect is checked before connect.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}
_ALLOWED_PORTS = {80, 443}
_MAX_REDIRECTS = 3
_MAX_BYTES = 200_000


class SsrfError(ValueError):
    pass


def _address_is_public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def public_http_url(value: str | None) -> str | None:
    """Return the URL when its literal form is acceptable. This does not resolve DNS."""
    try:
        _parse_public_url(value or "")
    except SsrfError:
        return None
    return urlparse((value or "").strip()).geturl()


def _parse_public_url(value: str):
    parsed = urlparse((value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SsrfError("scheme or host is not allowed")
    if parsed.username or parsed.password:
        raise SsrfError("userinfo is not allowed")
    host = parsed.hostname.strip(".").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise SsrfError("host is not allowed")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port not in _ALLOWED_PORTS:
        raise SsrfError("port is not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not _address_is_public(host):
        raise SsrfError("address is not public")
    return parsed


def default_resolver(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None)
    found = []
    for info in infos:
        address = info[4][0]
        if address not in found:
            found.append(address)
    return found


def safe_fetch(
    url: str,
    *,
    resolver=default_resolver,
    opener,
    max_redirects: int = _MAX_REDIRECTS,
    max_bytes: int = _MAX_BYTES,
    timeout: float = 5.0,
) -> bytes:
    """Fetch ``url`` through ``opener(url, addresses, timeout, max_bytes)``.

    ``opener`` must connect to the checked addresses, not a second lookup.
    It returns ``(status, location, body)``. Redirects are validated again.
    """
    current = url
    for _ in range(max_redirects + 1):
        parsed = _parse_public_url(current)
        host = parsed.hostname.strip(".").lower()
        try:
            addresses = list(resolver(host))
        except OSError as exc:
            raise SsrfError("name did not resolve") from exc
        if not addresses or any(not _address_is_public(item) for item in addresses):
            raise SsrfError("resolved address is not public")
        status, location, body = opener(current, addresses, timeout, max_bytes)
        if status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            continue
        if body is None:
            return b""
        raw = body if isinstance(body, bytes) else str(body).encode()
        if len(raw) > max_bytes:
            raise SsrfError("response is too large")
        return raw
    raise SsrfError("too many redirects")
