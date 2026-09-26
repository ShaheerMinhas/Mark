from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_FAKE_DOMAINS = {
    "example.com",
    "email.com",
    "test.com",
    "sentry.io",
    "wixpress.com",
    "github.com",
    "githubusercontent.com",
    "facebook.com",
    "fb.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
}

_JUNK_LOCAL = {
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "notifications",
    "support",
    "privacy",
    "legal",
    "webmaster",
    "postmaster",
}

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def name_parts(name: str) -> tuple[str, str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", name or "") if len(w) > 1]
    if not words:
        return "", ""
    first = words[0]
    last = words[-1] if len(words) > 1 else ""
    return first, last


def email_fits_person(email: str, name: str) -> bool:
    """True when the local-part looks like this person's name."""
    if not email or "@" not in email or not name:
        return False
    local_raw = email.split("@", 1)[0].lower()
    if re.search(r"facebook|linkedin|instagram|noreply", local_raw):
        return False
    local = re.sub(r"[^a-z]", "", local_raw)
    first, last = name_parts(name)
    if not first or len(local) < 2:
        return False
    if local == first or local.startswith(first):
        return True
    if last and (local == last or local.startswith(last) or local.endswith(last)):
        return True
    if last and local in {first[0] + last, first + last[0], first + last}:
        return True
    return False


def is_plausible_email(email: str) -> bool:
    value = (email or "").strip()
    if not _EMAIL_RE.fullmatch(value):
        return False
    if value.lower().endswith(_IMAGE_SUFFIXES):
        return False
    local, _, domain = value.partition("@")
    if domain.lower() in _FAKE_DOMAINS:
        return False
    if local.lower().split("+", 1)[0] in _JUNK_LOCAL:
        return False
    if domain.lower() in {"linkedin.com", "facebook.com", "twitter.com"}:
        return False
    if re.search(r"facebook|linkedin|instagram|noreply", local.lower()):
        return False
    return True


def extract_emails(text: str, *, name: str = "", require_name_match: bool = False) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _EMAIL_RE.finditer(text):
        email = match.group(0)
        key = email.lower()
        if key in seen or not is_plausible_email(email):
            continue
        if require_name_match and name and not email_fits_person(email, name):
            continue
        seen.add(key)
        found.append(email)
    return found


def extract_first_email(text: str, *, name: str = "") -> str | None:
    emails = extract_emails(text, name=name, require_name_match=bool(name))
    if emails:
        return emails[0]
    if name:
        emails = extract_emails(text, name=name, require_name_match=False)
    return emails[0] if emails else None
