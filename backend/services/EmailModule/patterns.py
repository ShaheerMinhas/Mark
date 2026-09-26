"""Company email-pattern detection and candidate generation.

Inferred addresses are never labeled FOUND or VERIFIED here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from collections import Counter

from backend.services.EmailModule.extract import is_plausible_email, name_parts

_PATTERN_FNS: dict[str, Callable[[str, str], str]] = {
    "first": lambda f, l: f,
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "flast": lambda f, l: f"{f[0]}{l}" if f else "",
    "f.last": lambda f, l: f"{f[0]}.{l}" if f else "",
    "firstl": lambda f, l: f"{f}{l[0]}" if l else "",
    "last.first": lambda f, l: f"{l}.{f}",
    "lastfirst": lambda f, l: f"{l}{f}",
    "first_last": lambda f, l: f"{f}_{l}",
}

SUPPORTED_PATTERNS = tuple(_PATTERN_FNS.keys())


def normalize_part(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def split_name(name: str) -> tuple[str, str]:
    first, last = name_parts(name)
    return normalize_part(first), normalize_part(last)


def email_local_domain(email: str) -> tuple[str, str]:
    value = (email or "").strip().lower()
    if "@" not in value:
        return "", ""
    local, domain = value.split("@", 1)
    return re.sub(r"[^a-z0-9._-]", "", local), domain


def match_pattern(email: str, first: str, last: str) -> str | None:
    local, _domain = email_local_domain(email)
    first = normalize_part(first)
    last = normalize_part(last)
    if not local or not first:
        return None
    for key, fn in _PATTERN_FNS.items():
        if not last and key not in {"first"}:
            continue
        try:
            if fn(first, last) == local:
                return key
        except Exception:
            continue
    return None


def generate_from_pattern(pattern: str, first: str, last: str, domain: str) -> str | None:
    first = normalize_part(first)
    last = normalize_part(last)
    domain = (domain or "").strip().lower().removeprefix("www.")
    fn = _PATTERN_FNS.get(pattern)
    if not fn or not first or not domain:
        return None
    if pattern != "first" and not last:
        return None
    local = fn(first, last)
    if not local:
        return None
    email = f"{local}@{domain}"
    return email if is_plausible_email(email) else None


def detect_pattern(examples: list[tuple[str, str]]) -> tuple[str | None, float, int]:
    """examples: list of (full_name, email). Returns (pattern, confidence 0-1, sample_count)."""
    votes: Counter[str] = Counter()
    supporting: list[str] = []
    for name, email in examples:
        if not is_plausible_email(email or ""):
            continue
        first, last = split_name(name)
        key = match_pattern(email, first, last)
        if key:
            votes[key] += 1
            supporting.append(email.lower())
    if not votes:
        return None, 0.0, 0
    pattern, count = votes.most_common(1)[0]
    total = sum(votes.values())
    agreement = count / max(total, 1)
    if count >= 3 and agreement >= 0.67:
        confidence = min(0.95, 0.55 + 0.1 * count)
    elif count >= 2 and agreement >= 0.5:
        confidence = 0.7
    else:
        confidence = 0.35
    if len(votes) > 1:
        confidence *= 0.75
    return pattern, round(confidence, 2), count


def generate_candidates(
    name: str,
    domain: str,
    patterns: list[tuple[str, float]],
) -> list[dict[str, object]]:
    first, last = split_name(name)
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    ranked = sorted(patterns, key=lambda item: item[1], reverse=True)
    for pattern, confidence in ranked:
        email = generate_from_pattern(pattern, first, last, domain)
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(
            {
                "email": email,
                "status": "inferred",
                "pattern": pattern,
                "pattern_confidence": confidence,
            }
        )
    return out
