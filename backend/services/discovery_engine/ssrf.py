"""Outbound URL policy for discovery fetches.

The autonomous company-page lookup and the interactive search client call
``safe_fetch`` so every address and every redirect is checked before connect.
Older email-module page fetchers still use their own HTTP client. Provider APIs
are fixed vendor hosts, not operator-supplied URLs.
"""

from __future__ import annotations

import gzip
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlparse

_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}
_ALLOWED_PORTS = {80, 443}
_MAX_REDIRECTS = 3
_MAX_BYTES = 200_000

#: Official search APIs the LinkedIn discovery path may call. Each request still
#: passes ``allowed_hosts`` of the one host it is about to open.
SEARCH_API_HOSTS = frozenset(
    {
        "www.googleapis.com",
        "api.search.brave.com",
        "google.serper.dev",
        "serpapi.com",
    }
)


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


def _header_token(value: str) -> str:
    return "".join(ch for ch in str(value) if 32 <= ord(ch) < 127)


def _read_chunks(data: bytes) -> bytes:
    out = bytearray()
    rest = data
    while rest:
        line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        token = line.split(b";", 1)[0].strip()
        if not token:
            break
        try:
            size = int(token, 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        rest = rest[size:]
        if rest.startswith(b"\r\n"):
            rest = rest[2:]
    return bytes(out)


def decode_http_body(body: bytes, headers: dict[str, str] | None) -> bytes:
    """Undo chunked framing and gzip when the response says so. Other bodies stay as read."""
    headers = headers or {}
    raw = body or b""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raw = _read_chunks(raw)
    encoding = headers.get("content-encoding", "").lower()
    if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    return raw


def pinned_opener(
    url: str,
    addresses: list[str],
    timeout: float,
    max_bytes: int,
    *,
    user_agent: str = "MarkBot/1.0",
    extra_headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
):
    """Connect to a checked address. The Host header and TLS name stay the original host.

    Redirects are not followed here. ``safe_fetch`` checks the next URL itself.
    ``method`` is GET or POST. A POST body is sent with Content-Length.
    """
    method_name = (method or "GET").upper()
    if method_name not in {"GET", "POST"}:
        raise SsrfError("method is not allowed")
    payload_body = body or b""
    if method_name == "GET":
        payload_body = b""
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
        header_lines = [
            f"{method_name} {path} HTTP/1.1",
            f"Host: {host}",
            f"User-Agent: {_header_token(user_agent) or 'MarkBot/1.0'}",
            "Accept: text/html,application/json",
            "Connection: close",
        ]
        if payload_body:
            header_lines.append(f"Content-Length: {len(payload_body)}")
        for key, value in (extra_headers or {}).items():
            name = _header_token(key)
            token = _header_token(value)
            if not name or name.lower() in {"host", "connection", "content-length"}:
                continue
            header_lines.append(f"{name}: {token}")
        payload = ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii", errors="strict")
        sock.sendall(payload + payload_body)
        data = b""
        while len(data) <= max_bytes + 65536:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    head, _, raw_body = data.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError) as exc:
        raise SsrfError("response status was not readable") from exc
    location = None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
        if key.lower() == "location":
            location = value.strip()
    response_body = decode_http_body(raw_body, headers)
    if len(response_body) > max_bytes:
        response_body = response_body[:max_bytes]
    return status, location, response_body, headers


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
        result = opener(current, addresses, timeout, max_bytes)
        status, location, body = result[0], result[1], result[2]
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
