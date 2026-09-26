"""Local verification: syntax, MX, disposable, role-based.

Does not SMTP-probe mailboxes. Catch-all/valid come only from a provider.
"""

from __future__ import annotations

import socket

from backend.services.EmailModule.extract import is_plausible_email
from backend.services.EmailModule.types import VerifyStatus, utc_now

_DISPOSABLE = {
    "mailinator.com",
    "guerrillamail.com",
    "10minutemail.com",
    "tempmail.com",
    "yopmail.com",
    "trashmail.com",
    "getnada.com",
    "sharklasers.com",
}

_ROLE_LOCALS = {
    "info",
    "support",
    "sales",
    "admin",
    "contact",
    "hello",
    "office",
    "help",
    "billing",
    "noreply",
    "no-reply",
    "team",
    "jobs",
    "hr",
    "press",
    "webmaster",
    "marketing",
}


def domain_of(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].lower().removeprefix("www.")


def local_of(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[0].lower()


def has_mx(domain: str) -> str:
    """Return ok, none, or unknown. Never treated as 'this mailbox exists'."""
    host = (domain or "").strip().lower()
    if not host:
        return "none"
    try:
        import dns.resolver  # type: ignore

        answers = dns.resolver.resolve(host, "MX")
        return "ok" if answers else "none"
    except ImportError:
        pass
    except Exception:
        pass
    try:
        socket.getaddrinfo(host, 25)
        return "unknown"
    except Exception:
        return "none"


def local_verify(email: str) -> dict[str, str]:
    if not is_plausible_email(email or ""):
        return {
            "status": VerifyStatus.INVALID.value,
            "mx": "none",
            "detail": "failed syntax or blocked domain",
            "checked_at": utc_now(),
            "provider": "local",
        }
    domain = domain_of(email)
    local = local_of(email).split("+", 1)[0]
    if domain in _DISPOSABLE:
        return {
            "status": VerifyStatus.DISPOSABLE.value,
            "mx": has_mx(domain),
            "detail": "disposable domain",
            "checked_at": utc_now(),
            "provider": "local",
        }
    if local in _ROLE_LOCALS:
        return {
            "status": VerifyStatus.ROLE_BASED.value,
            "mx": has_mx(domain),
            "detail": "role-based local part",
            "checked_at": utc_now(),
            "provider": "local",
        }
    mx = has_mx(domain)
    return {
        "status": VerifyStatus.UNKNOWN.value,
        "mx": mx,
        "detail": f"mx={mx}",
        "checked_at": utc_now(),
        "provider": "local",
    }


def normalize_provider_verify(raw: str | None) -> str:
    value = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if value in {"valid", "deliverable", "ok", "safe", "true"}:
        return VerifyStatus.VALID.value
    if value in {"invalid", "undeliverable", "false", "reject"}:
        return VerifyStatus.INVALID.value
    if value in {"accept_all", "acceptall", "catch_all", "catchall"}:
        return VerifyStatus.ACCEPT_ALL.value
    if value in {"disposable", "temporary"}:
        return VerifyStatus.DISPOSABLE.value
    if value in {"role", "role_based", "rolebased"}:
        return VerifyStatus.ROLE_BASED.value
    if value in {"unknown", "risky", "unverifiable", ""}:
        return VerifyStatus.UNKNOWN.value
    if value in {"blocked", "error", "timeout"}:
        return VerifyStatus.BLOCKED.value
    return VerifyStatus.UNKNOWN.value
