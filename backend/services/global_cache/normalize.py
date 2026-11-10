"""Normalize public prospect fields. Unknown values stay null."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from backend.services.identity import normalize_company, normalize_linkedin_url, normalize_name, normalize_title

_COUNTRIES = {
    "us": "US",
    "usa": "US",
    "united states": "US",
    "united states of america": "US",
    "gb": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "de": "DE",
    "germany": "DE",
    "fr": "FR",
    "france": "FR",
    "nl": "NL",
    "netherlands": "NL",
    "ie": "IE",
    "ireland": "IE",
    "se": "SE",
    "sweden": "SE",
    "no": "NO",
    "norway": "NO",
    "dk": "DK",
    "denmark": "DK",
    "fi": "FI",
    "finland": "FI",
    "ch": "CH",
    "switzerland": "CH",
    "at": "AT",
    "austria": "AT",
    "be": "BE",
    "belgium": "BE",
    "es": "ES",
    "spain": "ES",
    "it": "IT",
    "italy": "IT",
    "pt": "PT",
    "portugal": "PT",
    "pl": "PL",
    "poland": "PL",
    "cz": "CZ",
    "czechia": "CZ",
    "czech republic": "CZ",
    "ee": "EE",
    "estonia": "EE",
    "lt": "LT",
    "lithuania": "LT",
    "lv": "LV",
    "latvia": "LV",
    "ro": "RO",
    "romania": "RO",
}

_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico", "guam",
}

_SENIORITY = (
    ("c-level", ("chief ", "ceo", "cto", "cio", "cfo", "coo", "founder", "co-founder", "cofounder")),
    ("vp", ("vice president", " vp", "vp ", "svp", "evp")),
    ("director", ("director", "head of")),
    ("manager", ("manager", "lead ")),
    ("individual", ("engineer", "developer", "designer", "analyst")),
)

_FUNCTIONS = (
    ("engineering", ("engineer", "developer", "software", "cto")),
    ("it", ("information technology", "head of it", " it ", "cio")),
    ("product", ("product",)),
    ("data", ("data", "machine learning", "ml ", "artificial intelligence")),
    ("security", ("security", "cyber")),
    ("devops", ("devops", "sre", "cloud", "infrastructure")),
    ("sales", ("sales", "business development", "account executive")),
    ("marketing", ("marketing", "growth")),
    ("operations", ("operations", "coo")),
    ("management", ("ceo", "founder", "managing director")),
)


def country_code(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.strip().lower().replace(".", " ").split())
    if text in _COUNTRIES:
        return _COUNTRIES[text]
    if text in _US_STATES:
        return "US"
    if len(text) == 2 and text.isalpha():
        return text.upper() if text.upper() in set(_COUNTRIES.values()) else None
    for token in re.split(r"[,/|]", text):
        part = token.strip()
        if part in _COUNTRIES:
            return _COUNTRIES[part]
        if part in _US_STATES:
            return "US"
    return None


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if "://" in raw or raw.startswith("www."):
        if "://" not in raw:
            raw = "https://" + raw
        host = urlparse(raw).netloc or urlparse(raw).path
    else:
        host = raw.split("/")[0]
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    host = host.strip(".")
    if "." not in host or " " in host:
        return None
    return host


def normalize_technology(value: str) -> str | None:
    text = normalize_name(value)
    return text or None


def split_name(full_name: str) -> tuple[str | None, str | None]:
    parts = [p for p in normalize_name(full_name).split(" ") if p]
    if len(parts) < 2:
        return (parts[0] if parts else None, None)
    return parts[0], parts[-1]


def seniority_from_title(title: str | None) -> str | None:
    folded = f" {normalize_title(title or '')} "
    if not folded.strip():
        return None
    for label, tokens in _SENIORITY:
        if any(token in folded for token in tokens):
            return label
    return None


def function_from_title(title: str | None) -> str | None:
    folded = f" {normalize_title(title or '')} "
    if not folded.strip():
        return None
    for label, tokens in _FUNCTIONS:
        if any(token in folded for token in tokens):
            return label
    return None


def clean_email_status(value: str | None) -> str | None:
    from backend.services.global_cache.policy import KNOWN_EMAIL_STATUSES

    if not value:
        return None
    status = value.strip().lower()
    return status if status in KNOWN_EMAIL_STATUSES else None
