"""Phone number extraction from free-text snippets and HTML."""

from __future__ import annotations

import re

_PHONE_RE = re.compile(r"(\+?\d[\d\s()./\-]{7,}\d)")
#: Year-like or zip-like digit runs that look like phones but are not.
_FALSE_POSITIVE = re.compile(
    r"^(?:20\d{2})+$|^(?:19\d{2})+$|^\d{4,5}$"
)


def normalize_phone(raw: str) -> str | None:
    """Return a cleaned display form, or None if the string is not a usable phone."""
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw).strip()
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 10 or len(digits) > 15:
        return None
    if _FALSE_POSITIVE.fullmatch(digits):
        return None
    # LinkedIn activity years strung together (20202021…).
    if digits.startswith("20") and len(digits) <= 12 and re.fullmatch(r"(?:20\d{2})+", digits):
        return None
    return cleaned


def extract_phones(text: str, *, limit: int = 5) -> list[str]:
    """Pull plausible phone numbers from arbitrary text, preserving order."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _PHONE_RE.finditer(text):
        phone = normalize_phone(match.group(1))
        if not phone:
            continue
        key = re.sub(r"\D", "", phone)
        if key in seen:
            continue
        seen.add(key)
        found.append(phone)
        if len(found) >= limit:
            break
    return found
