from __future__ import annotations

import re
from typing import Any

from backend.services.CompanyModule.types import CompanyProfile
from backend.services.CompanyModule.web import company_website, search_company_location

_GENERIC = {
    "",
    "solar",
    "energy",
    "marketing",
    "retired",
    "consultant",
    "consulting",
    "growth advisor",
    "self-employed",
    "freelance",
    "independent",
    "n/a",
    "unknown",
    "company",
    "business",
    "chief",
    "brand",
    "battery",
    "host",
    "host of",
    "officer",
    "growth",
    "chief brand",
    "chief growth officer",
    "cdmp",
}

_TITLEISH = re.compile(
    r"(?i)\b(manager|director|officer|engineer|president|specialist|coordinator|"
    r"head of|vice president|\bvp\b|cmo|ceo|cfo|cto|cgo|brand strategy|"
    r"co-?owner|chairman|award-winning|professor|lecturer|dean|faculty|"
    r"registrar|instructor)\b"
)
_PRODUCTISH = re.compile(r"(?i)\b(robot|robots|youtuber|educator|packaging|pro)\b")
#: Long university / school names are real employers; the 4-word heuristic below
#: used to wipe "University of Management and Technology" to empty and tank
#: education searches (company blank → wrong matches, weak qualification).
_EDUCATION_ORG = re.compile(
    r"(?i)\b(university|universit(?:y|ät)|college|institute|school|academy|"
    r"polytechnic|nuces|nust|lums|umt|ucp|\buet\b|fast)\b"
)


def is_usable_company(name: str) -> bool:
    cleaned = re.sub(r"\s+", " ", name or "").strip(" ·|-–,;:()[]{}\"'")
    if len(cleaned) < 3 or len(cleaned) > 80:
        return False
    if re.fullmatch(r"[A-Za-z]{1,2}", cleaned):
        return False
    if cleaned.lower() in _GENERIC:
        return False
    if re.search(r"(?i)^(award-winning|building|helping|i revenue|& |host of|accelerating)\b", cleaned):
        return False
    if _TITLEISH.search(cleaned) and not _EDUCATION_ORG.search(cleaned):
        return False
    if _PRODUCTISH.search(cleaned) and not re.search(r"\b(Inc\.?|LLC|Ltd\.?|Corp\.?)\b", cleaned, re.I):
        return False
    words = re.findall(r"[a-z0-9]+", cleaned.lower())
    if (
        len(words) >= 4
        and not re.search(r"\b(inc|llc|ltd|corp|company|energy|solar|group)\b", cleaned, re.I)
        and not _EDUCATION_ORG.search(cleaned)
    ):
        return False
    return True


def lookup_company(
    name: str,
    *,
    ddgs: Any | None = None,
    skip_location: bool = False,
    skip_domain: bool = False,
) -> CompanyProfile | None:
    """Public domain + HQ lookup for one employer."""
    if not is_usable_company(name):
        return None
    domain, website = ("", "") if skip_domain else company_website(name, ddgs=ddgs)
    location, loc_evidence = ("", "") if skip_location else search_company_location(name, ddgs=ddgs)
    if not domain and not location:
        return None
    source = "web"
    evidence_bits = [bit for bit in [f"domain {domain}" if domain else "", loc_evidence] if bit]
    return CompanyProfile(
        name=name.strip(),
        domain=domain,
        website=website,
        location=location,
        source=source,
        evidence="; ".join(evidence_bits),
    )
