"""Stable identity keys for discovered people, used for deduplication.

A candidate's identity comes from its normalized LinkedIn profile URL when one is
available. Only when there is no profile URL do we fall back to a name/company/title
hash — the fallback deliberately requires all three signals so that two different
people who happen to share a name are not merged.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import unquote, urlparse, urlunparse

_PERSON_PATH = re.compile(r"/(?:in|pub)/([^/?#]+)", re.I)
_RESERVED_SLUGS = {"dir", "search", "results", "edit", "unavailable"}
_PUNCT = re.compile(r"[^a-z0-9]+")
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|plc|corp|corporation|co|company|"
    r"gmbh|ab|as|oy|bv|nv|sa|sas|srl|pvt|pty|group|holdings|holding)\b",
    re.I,
)


def normalize_linkedin_url(url: str) -> str:
    """Canonical ``https://www.linkedin.com/in/<slug>`` form, or "" if not a person URL.

    Drops tracking parameters, fragments, trailing slashes and locale subdomains
    (``de.linkedin.com`` and ``www.linkedin.com`` resolve to the same person).
    """
    if not url:
        return ""
    raw = url.strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif not re.match(r"^https?://", raw, re.I):
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if not host.endswith("linkedin.com"):
        return ""
    match = _PERSON_PATH.search(unquote(parsed.path or ""))
    if not match:
        return ""
    slug = match.group(1).strip("/").lower()
    if not slug or slug in _RESERVED_SLUGS:
        return ""
    return urlunparse(("https", "www.linkedin.com", f"/in/{slug}", "", "", ""))


def _fold(value: str) -> str:
    """Lowercase, strip accents, collapse punctuation to single spaces."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _PUNCT.sub(" ", stripped.lower()).strip()


def normalize_name(value: str) -> str:
    return _fold(value)


def normalize_company(value: str) -> str:
    folded = _fold(value)
    if not folded:
        return ""
    without_suffix = _COMPANY_SUFFIX.sub(" ", folded)
    return re.sub(r"\s+", " ", without_suffix).strip() or folded


def normalize_title(value: str) -> str:
    return _fold(value)


def identity_key(
    *,
    linkedin_url: str = "",
    name: str = "",
    company: str = "",
    title: str = "",
) -> str:
    """Return a dedup key, or "" when the candidate cannot be identified safely."""
    profile = normalize_linkedin_url(linkedin_url)
    if profile:
        return f"li:{profile.rsplit('/', 1)[-1]}"

    person = normalize_name(name)
    employer = normalize_company(company)
    role = normalize_title(title)
    if not person or not employer or not role:
        return ""
    digest = hashlib.sha256(f"{person}|{employer}|{role}".encode()).hexdigest()
    return f"nct:{digest[:40]}"
