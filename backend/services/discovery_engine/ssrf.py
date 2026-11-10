"""Outbound URL policy for new discovery fetches.

Legacy search-engine and public-email clients do not use this module. Provider
APIs are fixed vendor hosts, not operator-supplied URLs. A new fetch must call
``safe_fetch`` so every address and every redirect is checked before connect.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
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


def pinned_opener(url: str, addresses: list[str], timeout: float, max_bytes: int):
    """Connect to a checked address. The Host header and TLS name stay the original host.

    Redirects are not followed here. ``safe_fetch`` checks the next URL itself.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    port = 443 if parsed.scheme == "https" else 80
    sock = socket.create_connection((addresses[0], port), timeout=timeout)
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        payload = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: MarkBot/1.0\r\n"
            "Accept: text/html\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(payload.encode("ascii"))
        data = b""
        while len(data) <= max_bytes + 65536:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError) as exc:
        raise SsrfError("response status was not readable") from exc
    location = None
    for line in lines[1:]:
        if line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
    if len(body) > max_bytes:
        body = body[:max_bytes]
    return status, location, body


def safe_fetch(
    url: str,
    *,
    resolver=default_resolver,
    opener,
    allowed_hosts: set[str] | None = None,
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
        if allowed_hosts is not None and host not in allowed_hosts:
            raise SsrfError("host is outside the allowed set")
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
