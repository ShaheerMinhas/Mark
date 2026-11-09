"""Marketing lead discovery — LinkedIn people at businesses (via open-web search)."""

from __future__ import annotations

import difflib
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from backend.services.CompanyModule.extract import extract_profile_geo
from backend.services.CompanyModule.finder import is_usable_company
from backend.services.ddg import ddg_session, ddg_text


INDUSTRIES = [
    "Technology",
    "Healthcare",
    "Finance",
    "Retail",
    "Education",
    "Real Estate",
    "Manufacturing",
    "Hospitality",
    "Marketing & Advertising",
    "Professional Services",
]

COMPANY_SIZES = [
    "1-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1000",
    "1000+",
]

INCOME_LEVELS = [
    "Budget-conscious",
    "Mid-market",
    "Premium",
    "Enterprise",
]

# Real activity language — not job titles or "500+ connections"
_ACTIVE_CUES = (
    "posted",
    "shared",
    "commented",
    "published",
    "speaking",
    "speaker",
    "hiring",
    "open to work",
    "actively",
)

_BUSINESS_CUES = (
    "founder",
    "co-founder",
    "ceo",
    "owner",
    "inc.",
    "llc",
    "ltd",
    "corporation",
    "company",
    "studio",
    "agency",
)

# Placeholders / junk we never want to show as a company name
_GENERIC_COMPANIES = {
    "",
    "business",
    "company",
    "unknown",
    "n/a",
    "na",
    "none",
    "linkedin",
    "linkedin member",
    "professional",
    "self-employed",
    "self employed",
    "freelancer",
    "independent",
    "not specified",
    "general",
    "the",
    "a",
    "an",
    "their",
    "his",
    "her",
    "our",
    "my",
    "solar",
    "energy",
    "chief",
    "brand",
    "battery",
    "host",
    "host of",
    "officer",
}

# Common false positives after the word "at"
_AT_STOPWORDS = {
    "the", "a", "an", "his", "her", "their", "our", "my", "linkedin", "least",
    "home", "work", "scale", "ease", "heart", "hand", "night", "university",
    "college", "school", "least", "present", "this", "that", "which",
    "host", "chief", "at",
    # Conjunctions and prepositions: a company string starting with one of these
    # is a fragment of a longer job title, not an employer ("and Commercial",
    # "EMEA hos"). "hos"/"bei"/"chez" are the local "at" in Nordic/DE/FR profiles.
    "and", "or", "of", "for", "with", "in", "on", "to", "from", "by", "as",
    "hos", "bei", "chez", "presso", "en",
}

_ROLE_WORDS = {
    "founder",
    "co-founder",
    "cofounder",
    "ceo",
    "cto",
    "cmo",
    "cfo",
    "coo",
    "owner",
    "president",
    "director",
    "manager",
    "head",
    "vp",
    "vice",
    "partner",
    "consultant",
    "engineer",
    "developer",
    "designer",
    "analyst",
    "specialist",
    "lead",
    "intern",
    "student",
    "professional",
    "marketing",
    "marketer",
    "cgo",
    "chair",
    "professor",
    "lecturer",
    "dean",
    "faculty",
    "registrar",
    "chairman",
    "instructor",
    "researcher",
    "fellow",
}

# High-confidence job titles used before "at <Company>"
_JOB_TITLE_RE = (
    r"(?:Co-?Founder|Founder|Co-?Owner|CEO|CTO|CMO|CFO|COO|CGO|Owner|President|Director|"
    r"Managing Director|General Manager|"
    r"(?:Principal |Principle |Senior |Staff |Lead |Junior |Assistant |Associate |Tenured )?"
    r"(?:Software |Data |Platform |DevOps |IT |Enterprise )?"
    r"(?:Engineer|Developer|Designer|Consultant|Analyst|Architect|Specialist|Manager|"
    r"Professor|Lecturer|Dean|Registrar|Instructor|Researcher|Fellow)|"
    r"Marketing Manager|Marketing Director|Brand Manager|Product Manager|Sales Manager|Project Manager|"
    r"Head of Marketing|Head of Growth|Head of Communications|"
    r"Head of Department|Vice Chancellor|Controller of Examinations|"
    r"Marketer|Solar Marketer|"
    r"Growth Lead|Head of [A-Za-z& ]{2,30}|"
    r"VP(?:[\s\-–]+(?:of\s+)?[A-Za-z][\w&.''\-]*(?:\s+[A-Za-z][\w&.''\-]*){0,6})?|"
    r"Vice President(?:\s+of)?(?:\s+[A-Za-z&][A-Za-z& ]{1,30})?|"
    r"Board Chair|Partner|Principal|Lead)"
)

_KNOWN_INDUSTRIES = {item.lower() for item in INDUSTRIES}

_US_STATE_ABBR = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
        "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
        "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
        "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    }
)

_SERP_JUNK = re.compile(
    r"(?i)(?:wikipedia|yahoo|sign in|log\s?in|facebook|meta for business|"
    r"wireless phone|total commander|practo|at sign|linkedinat)"
)
# Accented Latin letters are included deliberately: an ASCII-only class truncates
# "Lina Stenesjö" to "Lina Stenesj" and leaks the "ö" into the parsed job title,
# which corrupts every Nordic, German, French, and Iberian name.
_NAME_UPPER = "A-ZÀ-ÖØ-ÞĀ-Ž"
_NAME_LOWER = "a-zß-öø-ÿāāž"
_PERSON_NAME_RE = re.compile(
    r"^("
    rf"[{_NAME_UPPER}][{_NAME_LOWER}{_NAME_UPPER}'’\-]+"
    rf"(?:\s+[{_NAME_UPPER}]\.)?"
    rf"(?:\s+[{_NAME_UPPER}][{_NAME_LOWER}{_NAME_UPPER}'’\-]+){{0,3}}"
    r")"
)


@dataclass
class Lead:
    name: str
    title: str
    company: str
    industry: str
    location: str
    linkedin_url: str
    website: str
    email_guess: str
    age_range: str
    relevance_score: float
    activity_status: str
    business_associated: bool
    snippet: str
    source: str
    tags: list[str]


def _domain_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host.removeprefix("www.")
    except Exception:
        return ""


def _normalize_linkedin_url(url: str) -> str:
    """Keep only person profile URLs (linkedin.com/in/...)."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        if "linkedin.com" not in host:
            return ""
        path = parsed.path or ""
        # Drop locale prefixes like /in/ or /pub/
        m = re.search(r"/(in|pub)/([^/?#]+)", path, re.I)
        if not m:
            return ""
        slug = m.group(2).strip("/")
        if not slug or slug.lower() in {"dir", "search"}:
            return ""
        return urlunparse(("https", "www.linkedin.com", f"/in/{slug}", "", "", ""))
    except Exception:
        return ""


def _is_linkedin_person(url: str) -> bool:
    return bool(_normalize_linkedin_url(url))


#: Bare place names that a scraped "company" sometimes collapses to. Matched only
#: as the entire string, since several are legitimate words inside a real company
#: name ("Nordic Freight", "Europe RPET"). Ambiguous country names that are also
#: common brands or surnames (Jordan, Chad) are deliberately omitted.
_BARE_PLACES = frozenset(
    {
        "sweden", "norway", "denmark", "finland", "iceland", "germany", "france",
        "spain", "italy", "portugal", "netherlands", "holland", "belgium",
        "austria", "switzerland", "poland", "czechia", "czech republic", "ireland",
        "scotland", "wales", "england", "united kingdom", "great britain",
        "canada", "australia", "new zealand", "india", "pakistan", "japan",
        "china", "singapore", "malaysia", "indonesia", "thailand", "vietnam",
        "brazil", "mexico", "argentina", "chile", "colombia", "peru",
        "south africa", "nigeria", "kenya", "egypt", "morocco", "israel",
        "turkey", "greece", "romania", "hungary", "bulgaria", "croatia",
        "slovakia", "slovenia", "estonia", "latvia", "lithuania", "ukraine",
        "russia", "saudi arabia", "qatar", "kuwait",
        "europe", "asia", "africa", "emea", "apac", "latam", "anz",
        "nordics", "nordic region", "scandinavia", "benelux", "dach",
        "middle east", "north america", "south america", "worldwide", "global",
    }
)


def _looks_like_location(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"\b(Inc\.?|LLC|Ltd\.?|Corp\.?|Corporation|Company)\b", t, re.I):
        return False
    if t.lower().strip(" ,.") in _BARE_PLACES:
        return True
    if re.search(
        r"\b(Area|Region|Metropolitan|Metro|Greater|County|Province|District|"
        r"United States|USA|U\.S\.|UK|Remote|Worldwide|Global)\b",
        t,
        re.I,
    ):
        return True
    # "Austin, TX" / "London, United Kingdom"
    if "," in t and len(t) < 60:
        return True
    if t.upper() in _US_STATE_ABBR:
        return True
    return False


_ROLE_COMPANY_SPLIT = re.compile(
    r"(?:"
    r"\s+at\s+"  # VP at Xavor
    r"|"
    r"\s+@\s+"  # VP @ Xavor
    r"|"
    r"\s+@(?=[A-Za-z])"  # VP @Xavor
    r")",
    re.I,
)
_EMAIL_IN_TEXT = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PROMPT_STOPWORDS = {
    "a", "an", "the", "in", "at", "of", "and", "or", "for", "to", "with",
    "who", "are", "is", "on", "from", "that", "this",
}


def _as_list(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_as_list(item))
        return out
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,;]+", text) if part.strip()]


def _or_clause(values: list[str]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = (raw or "").strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        parts.append(f'"{value}"' if (" " in value or "-" in value) else value)
        if len(parts) >= 6:
            break
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def _sanitize_serp_title(title: str) -> str:
    """Keep the first LinkedIn person result; drop glued SERP junk."""
    text = re.sub(r"\s+", " ", title or "").strip()
    if not text:
        return ""
    text = re.split(r"\|\s*LinkedIn\b", text, maxsplit=1, flags=re.I)[0]
    text = re.split(
        r"(?i)(?=LinkedInAt|\bWikipedia\b|\bSign in\b|\bLog ?in\b|Meta for Business|"
        r"Wireless Phone|Total Commander|At sign)",
        text,
        maxsplit=1,
    )[0]
    text = re.split(
        r"\s*\.{2,}\s*(?=[A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’.\-]+){0,3}\s*[-|])",
        text,
        maxsplit=1,
    )[0]
    text = re.split(
        r"\s*\.{2,}\s*(?=[A-Z][a-z]+(?:\s+[A-Z][a-zA-Z.]+){1,3}\b)",
        text,
        maxsplit=1,
    )[0]
    return text.strip(" ·|-–—,")


def _clean_person_name(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw or "").strip(" ·|-–—,")
    match = _PERSON_NAME_RE.match(text)
    if not match:
        return ""
    name = match.group(1).strip()
    lowered = name.lower()
    if len(name.split()) < 2:
        return ""
    if re.search(r"linkedin|wikipedia|yahoo|http|profile|followers|connections", lowered):
        return ""
    if re.search(
        r"(?i)\b(engineer|manager|director|founder|consultant|president|software|"
        r"intern|student|marketing|sales|\bvp\b|cmo|ceo)\b",
        name,
    ):
        return ""
    if sum(ch.isalpha() for ch in name) < 2 or len(name) > 60:
        return ""
    return name


def _looks_like_role(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 80 or len(cleaned.split()) > 10:
        return False
    tokens = re.findall(r"[a-z0-9]+", cleaned.lower())
    if any(token in _ROLE_WORDS or token.rstrip("s") in _ROLE_WORDS for token in tokens):
        return True
    return bool(re.search(rf"(?i)\b{_JOB_TITLE_RE}\b", cleaned))


def _clean_role(role: str) -> str:
    cleaned = re.sub(r"\s+", " ", role or "").strip(" ·|-–—,")
    cleaned = re.sub(r"^[™©®\s\-–—]+", "", cleaned)
    cleaned = re.sub(r"(?i)^award[- ]winning\s+", "", cleaned)
    cleaned = re.sub(r"\s*\|\s*LinkedIn.*$", "", cleaned, flags=re.I)
    if re.search(r"(?i)award-winning", cleaned):
        return ""
    if _SERP_JUNK.search(cleaned):
        cleaned = _SERP_JUNK.split(cleaned)[0].strip(" ·|-–—,")
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rsplit(" ", 1)[0]
    if re.search(r"(?i)\b(linkedin|wikipedia|followers|connections|yahoo|sign in)\b", cleaned):
        return ""
    return cleaned


def _split_role_at_company(text: str) -> tuple[str, str]:
    """Split 'VP-IT Services at/@ Xavor Corporation' into role + company."""
    if not text:
        return "", ""
    masked = _EMAIL_IN_TEXT.sub(" ", text)
    if not _ROLE_COMPANY_SPLIT.search(masked):
        return text.strip(), ""
    left, right = _ROLE_COMPANY_SPLIT.split(masked, maxsplit=1)
    left, right = left.strip(" ·|-–,"), right.strip(" ·|-–,;:()[]{}\"'")
    if right and (_looks_like_role(left) or (left and len(left.split()) <= 8)):
        return left, right
    return text.strip(), ""


def _split_leading_role(rest: str) -> tuple[str, str]:
    """Split 'CEO & Co-Founder SEG Solar, Inc. Houston' into role + remainder."""
    if not rest:
        return "", ""
    match = re.match(
        rf"(?i)^((?:{_JOB_TITLE_RE})(?:\s*&\s*(?:{_JOB_TITLE_RE}))*)\s+(.+)$",
        rest,
    )
    if match:
        return match.group(1).strip(), match.group(2).strip()
    match = re.match(r"(?i)^(Board Chair|Chair(?:man|woman)?)\s+(.+)$", rest)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", rest


def _split_comma_company(text: str) -> tuple[str, str]:
    if not text or "," not in text:
        return (text or "").strip(), ""
    left, right = text.rsplit(",", 1)
    left, right = left.strip(), right.strip()
    if not left or not right or len(right.split()) > 6:
        return text.strip(), ""
    if _looks_like_role(left) and _clean_company(right):
        return left, right
    return text.strip(), ""


def _clean_company(raw: str, *, name: str = "") -> str:
    """Normalize a scraped company string; return '' unless it looks like a real employer."""
    if not raw:
        return ""
    _role, after_at = _split_role_at_company(raw)
    if after_at:
        raw = after_at
    company = raw.strip(" ·|-–,;:()[]{}\"'")
    company = re.sub(r"\s+", " ", company)
    company = re.sub(r"\s*\|\s*LinkedIn.*$", "", company, flags=re.I).strip()
    company = re.sub(r"\s+on LinkedIn.*$", "", company, flags=re.I).strip()
    if " · " in company:
        company = company.split(" · ", 1)[0].strip()
    company = re.split(r"\s+[–—]\s+", company)[0].strip()
    company = re.split(r"\s+\|\s+", company)[0].strip()
    company = re.sub(
        r"(?:,\s*)?(?:Houston|Austin|Dallas|Texas|California|Florida|USA|"
        r"United States|Pakistan|India)\s*$",
        "",
        company,
        flags=re.I,
    ).strip(" ,")
    company = re.sub(r"(?i)^(?:at|@)\s+", "", company).strip()
    if "..." in company:
        company = company.split("...")[0].strip()
    company = re.sub(r"\s*\([^)]*\)\s*$", "", company).strip()
    company = re.sub(r"\s*\([^)]*$", "", company).strip()

    if _EMAIL_IN_TEXT.fullmatch(company):
        return ""
    if re.search(r"\b(view|profile|connections?|followers?|see the complete|wikipedia)\b", company, re.I):
        return ""
    # Not word-bounded: SERP snippets run profiles together without a separator,
    # yielding strings like "LinkedInKarolin Henningsso".
    if "linkedin" in company.lower():
        return ""
    if _looks_like_location(company):
        return ""
    if len(company) < 3 or len(company) > 80:
        return ""
    lower = company.lower()
    if lower in _GENERIC_COMPANIES:
        return ""
    if name and lower == name.lower():
        return ""
    words = re.split(r"\s+", lower)
    # "university"/"college" are stopwords so bare "at University" is rejected,
    # but "University of Management and Technology" is a real employer.
    _edu_head = words[0] in {"university", "college", "school", "institute"} and len(words) >= 2
    if words[0] in _AT_STOPWORDS and not _edu_head:
        return ""
    # A trailing preposition means the match stopped mid-phrase ("EMEA hos").
    if len(words) > 1 and words[-1] in _AT_STOPWORDS:
        return ""
    if re.search(
        r"(?i)^(award-winning|building|helping|passionate|leading|experienced|"
        r"currently|responsible|board chair|host of|accelerating)\b",
        company,
    ):
        return ""
    words = re.findall(r"[a-z0-9]+", lower)
    if words and all(w in _ROLE_WORDS for w in words):
        return ""
    if re.search(r"\b(who|which|where|that|with|from|about|looking|based)\b", lower):
        return ""
    # _FOREIGN_ONLY includes "lahore"/"pakistan" for US-only marketing searches;
    # do not wipe real universities that legitimately mention those places.
    _edu = re.search(
        r"(?i)\b(university|college|institute|school|nust|lums|umt|ucp|uet|fast|nuces)\b",
        company,
    )
    if _FOREIGN_ONLY.search(company) and not _edu:
        return ""
    if not is_usable_company(company):
        return ""
    return company


def _company_from_role_at(text: str, *, name: str = "") -> str:
    """Accept 'Job Title at Company', including hyphenated titles like VP-IT Services."""
    if not text:
        return ""
    _role, company = _split_role_at_company(text)
    if company:
        return _clean_company(company, name=name)
    match = re.search(rf"(?i)\b{_JOB_TITLE_RE}\s+(?:at|@)\s*([^·|\n]+)", text)
    if not match:
        return ""
    return _clean_company(match.group(1), name=name)


def _trim_company_tail(text: str) -> str:
    """Cut a scraped company at the first sentence or clause boundary.

    Snippet patterns capture to end of line, so "Nordic Freight AB. Stockholm."
    arrives as one string and would otherwise be rejected wholesale.
    """
    cleaned = (text or "").strip()
    # A period that ends a sentence, as opposed to one inside "A.B." or "Inc."
    cleaned = re.split(r"\.\s+(?=[A-ZÀ-ÖØ-Þ])|\.$|\s+[·|]\s+|,\s+", cleaned)[0]
    return cleaned.strip(" ,.;:")


def _company_from_snippet(body: str, *, name: str = "") -> str:
    """Conservative snippet scrape — only high-confidence employer patterns."""
    if not body:
        return ""
    patterns = [
        # "Experience: University of Management and Technology - UMT"
        r"(?i)Experience:\s*((?:University|College|Institute|School)[^·|\n]{2,70})",
        r"(?i)Experience:\s*([^·|\n]{2,60})",
        rf"(?i)Experience[:\s]+{_JOB_TITLE_RE}\s+(?:at|@)\s*([^·|\n]+)",
        rf"(?i)\b{_JOB_TITLE_RE}\s+(?:at|@)\s*([^·|\n]+)",
        # LinkedIn renders experience as "Role. Employer. Mar 2024 - Present",
        # with no "at" to anchor on.
        rf"(?i)\b{_JOB_TITLE_RE}\s*[.·]\s*([{_NAME_UPPER}][^.·|\n]{{2,60}}?)\s*[.·]",
        # "Associate Professor, UMT, Pakistan" / "Professor at UMT Lahore"
        r"(?i)\b(?:Professor|Lecturer|Dean|Faculty)\b[^·|\n]{0,40}?\b(?:at|,)\s*((?:UMT|NUST|LUMS|UET|UCP|FAST|NUCES)[^·|\n,]{0,40})",
        r"^([A-Z][^·|\n]{2,50}(?:,?\s+(?:LLC|Inc\.?|Ltd\.?|Corp\.?)))\b",
        r"([A-Z][\w&.''\-]*(?:\s+(?:&|and|[A-Z][\w&.''\-]*)){0,4})\s*[·|]\s*(?:Full-?time|Part-?time|Contract)\b",
    ]
    for pat in patterns:
        match = re.search(pat, body)
        if match:
            cleaned = _clean_company(_trim_company_tail(match.group(1)), name=name)
            if cleaned:
                return cleaned
    return ""


def _role_from_snippet(body: str) -> str:
    if not body:
        return ""
    patterns = [
        rf"(?i)Experience[:\s]+({_JOB_TITLE_RE})",
        rf"(?i)(?:I(?:'m| am) an?|working as (?:an? )?|role of )\s*({_JOB_TITLE_RE})",
        rf"(?i)\b({_JOB_TITLE_RE})\s+(?:at|@)",
    ]
    for pat in patterns:
        match = re.search(pat, body)
        if match:
            role = _clean_role(match.group(1))
            if role:
                return role
    return ""


def _location_from_snippet(body: str) -> str:
    return extract_profile_geo(body or "")


def _parse_linkedin_title(title: str, body: str = "") -> tuple[str, str, str]:
    """Parse one LinkedIn person from a SERP title. Empty fields stay empty — never invent."""
    cleaned = _sanitize_serp_title(title)
    cleaned = re.sub(r"\s*\|\s*LinkedIn\s*$", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s*-\s*LinkedIn\s*$", "", cleaned, flags=re.I).strip()
    if not cleaned or _SERP_JUNK.search(cleaned) and _SERP_JUNK.search(cleaned).start() < 8:
        return "", "", ""

    name = _clean_person_name(cleaned)
    if not name:
        first = re.split(r"\s*[|·]\s*|\s+-\s+", cleaned, maxsplit=1)[0]
        name = _clean_person_name(first)
    if not name:
        return "", "", ""

    rest = cleaned[len(name):].strip(" ·|-–—,")
    rest = re.sub(r"(?i)^(she/her|he/him|they/them)\s*[-–|]\s*", "", rest)
    role, company = "", ""
    dash_parts = [part.strip() for part in re.split(r"\s+-\s+", rest) if part.strip()] if rest else []
    if len(dash_parts) >= 2 and _looks_like_role(dash_parts[0]):
        role = dash_parts[0]
        company = _clean_company(dash_parts[1], name=name) if not _looks_like_location(dash_parts[1]) else ""
        rest = ""
    segs = [part.strip() for part in re.split(r"\s*[|·]\s*", rest) if part.strip()] if rest else []
    if len(segs) >= 2:
        head, tail = segs[0], segs[1]
        left, right = _split_role_at_company(head)
        if right:
            role, company = left, right
        elif _looks_like_role(head):
            role = head
            company = _clean_company(tail, name=name) if not _looks_like_location(tail) else ""
        else:
            rest = head
            segs = []
    if not role and not company and rest:
        left, right = _split_role_at_company(rest)
        if right:
            role, company = left, right
        else:
            peeled_role, peeled_rest = _split_leading_role(rest)
            if peeled_role:
                role = peeled_role
                rest = peeled_rest
            left, right = _split_comma_company(rest)
            if right:
                role, company = (role or left), right
            else:
                # Prefer treating bare "Associate Professor" as a role, not an employer.
                if not role and _looks_like_role(rest):
                    role = rest
                else:
                    maybe_company = _clean_company(rest, name=name)
                    if maybe_company and not _looks_like_role(rest):
                        company = maybe_company
                    elif not role and _looks_like_role(rest):
                        role = rest

    role = _clean_role(role)
    company = _clean_company(company, name=name)
    if role and not company:
        peeled_role, peeled_rest = _split_leading_role(role)
        if peeled_role and peeled_rest:
            maybe = _clean_company(peeled_rest, name=name)
            if maybe:
                role, company = peeled_role, maybe
    if role and re.search(r"\s+(?:at|@)\s+", role, re.I):
        left, right = _split_role_at_company(role)
        if right:
            maybe = _clean_company(right, name=name)
            if maybe:
                role, company = _clean_role(left), maybe
    if not company and body:
        company = _company_from_snippet(body, name=name)
    if not role and body:
        role = _role_from_snippet(body)
    return name, _clean_role(role), _clean_company(company, name=name)


_MARKETING_CUE = re.compile(
    r"(?i)\b(marketing|marketer|marketers|cmo|cgo|brand manager|demand[- ]gen|"
    r"growth marketing|content marketing|digital marketing|product marketing|"
    r"communications|public relations|go-to-market)\b"
)
_US_CUE = re.compile(
    r"(?i)\b(usa|u\.s\.a?\.?|united states|america|north america|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    r"florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    r"louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    r"missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    r"new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    r"virginia|washington|west virginia|wisconsin|wyoming|"
    r"houston|austin|dallas|chicago|boston|seattle|denver|phoenix|atlanta|"
    r"miami|jacksonville|tampa|orlando|nashville|charlotte|raleigh|portland|"
    r"minneapolis|detroit|philadelphia|baltimore|pittsburgh|columbus|"
    r"indianapolis|milwaukee|sacramento|san francisco|los angeles|san diego|"
    r"san jose|las vegas|salt lake|kansas city|st\.? louis|brooklyn|manhattan|"
    r"queens|long island|orange county|silicon valley|bay area|metro area|"
    r"greater [a-z]+|new york city|\bnyc\b|washington dc|d\.c\.|"
    r"hoffman estates)\b"
)
_FOREIGN_ONLY = re.compile(
    r"(?i)\b(canada|lahore|karachi|india|pakistan|united kingdom|\buk\b|"
    r"germany|australia|china|singapore|dubai|nigeria|brasil|brazil|mexico|"
    r"spain|argentina|chile|poland|kenya|ghana|pty ltd|\bgmbh\b|"
    r"executivo com|gerente de marketing|gestión|berufserfahrung|"
    r"srne nigeria|australian-owned)\b"
)
_TITLE_ALIASES = {
    "marketting": "Marketing",
    "markting": "Marketing",
    "marketing": "Marketing",
    "marketer": "Marketer",
    "cmo": "CMO",
    "cgo": "CGO",
    "head of marketing": "Head of Marketing",
    "marketing manager": "Marketing Manager",
    "marketing director": "Marketing Director",
    "marketing agent": "Marketing Manager",
    "marketing agents": "Marketing Manager",
}
_MARKETING_TITLES = [
    "CMO",
    "Head of Marketing",
    "Marketing Manager",
    "Marketing Director",
    "Brand Manager",
    "Growth Lead",
]


def _wants_marketing(prompt: str, job_titles: list[str]) -> bool:
    blob = f"{prompt} {' '.join(job_titles)}".lower()
    return bool(_MARKETING_CUE.search(blob) or re.search(r"\bmarket", blob))


def _wants_usa(location: str, prompt: str) -> bool:
    blob = f"{location} {prompt}".lower()
    return bool(re.search(r"united states|\busa\b|u\.s\.a?\.?|\bamerica\b", blob))


def _canonical_job_titles(job_titles: list[str], prompt: str = "") -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(title: str) -> None:
        key = title.lower().strip()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(title)

    known_keys = list(_TITLE_ALIASES.keys())
    for raw in job_titles:
        key = raw.lower().strip()
        if key in _TITLE_ALIASES:
            add(_TITLE_ALIASES[key])
            continue
        close = difflib.get_close_matches(key, known_keys, n=1, cutoff=0.78)
        if close:
            add(_TITLE_ALIASES[close[0]])
            continue
        add(raw)
    if _wants_marketing(prompt, job_titles):
        for title in _MARKETING_TITLES:
            add(title)
    return out


def _sector_terms(prompt: str, industry: str, company: str) -> list[str]:
    terms = [item for item in _as_list(industry) + _as_list(company) if item]
    blob = f"{prompt} {' '.join(terms)}".lower()
    if "solar" in blob and "solar" not in {t.lower() for t in terms}:
        terms.append("solar")
    return terms


def _location_query_terms(location: str, prompt: str) -> list[str]:
    terms = _as_list(location)
    if _wants_usa(location, prompt):
        return ["USA", "United States"]
    return terms


def _has_marketing_cue(text: str) -> bool:
    return bool(_MARKETING_CUE.search(text or ""))


def _has_us_cue(text: str) -> bool:
    return bool(_US_CUE.search(text or ""))


def _serp_title_head(title: str) -> str:
    return re.split(r"\s*\|\s*", title or "", maxsplit=1)[0][:140]


def _experience_line(body: str) -> str:
    match = re.search(r"(?i)Experience:\s*([^·|\n]{2,60})", body or "")
    return match.group(1).strip() if match else ""


def _sector_blob(name: str, company: str, role: str, body: str) -> str:
    blob = f"{company} {role} {_experience_line(body)}"
    if name:
        blob = re.sub(re.escape(name), " ", blob, flags=re.I)
    return blob


def _matches_sector(sectors: list[str], *, name: str, company: str, role: str, body: str) -> bool:
    wanted = [item for item in sectors if item and item.lower() not in _KNOWN_INDUSTRIES]
    if not wanted:
        return True
    hay = f"{_sector_blob(name, company, role, body)} {(body or '')[:240]}".lower()
    return any(item.lower() in hay for item in wanted)


def _query_job_titles(job_titles: list[str], prompt: str) -> list[str]:
    titles = _canonical_job_titles(job_titles, prompt)
    out: list[str] = []
    seen: set[str] = set()

    def add(title: str) -> None:
        key = title.lower().strip()
        if not key or key in seen or key in {"marketing", "marketer"}:
            return
        seen.add(key)
        out.append(title)

    if _wants_marketing(prompt, job_titles) or any(t.lower() == "marketing" for t in titles):
        for title in (
            "Marketing Director",
            "Marketing Manager",
            "Head of Marketing",
            "CMO",
            "Director of Marketing",
            "VP of Marketing",
            "Brand Manager",
            "Demand Generation Manager",
        ):
            add(title)
    for title in titles:
        add(title)
    return out[:8]


_GENERIC_EMPLOYER = re.compile(
    r"(?i)\b(companies|businesses|firms|agencies|startups|organizations|people)\b"
)
_EMPLOYER_LOCATION = re.compile(
    r"(?i)\b(united states|usa|u\.s\.a?\.?|\bamerica\b|in the|pakistan|india|"
    r"united kingdom|\buk\b)\b"
)


def _named_employer_term(text: str) -> str:
    """Keep 'VP at Xavor' as a required employer; drop 'at solar companies in the US'."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return ""
    if _EMPLOYER_LOCATION.search(cleaned) or _GENERIC_EMPLOYER.search(cleaned):
        return ""
    if not is_usable_company(cleaned):
        return ""
    return " ".join(cleaned.split()[:3])


def _required_terms(prompt: str, *, companies: list[str], industries: list[str]) -> list[str]:
    terms: list[str] = []
    terms.extend(companies)
    _role, prompt_company = _split_role_at_company(prompt)
    named = _named_employer_term(prompt_company)
    if named:
        terms.append(named)
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower().strip()
        if key and key not in seen and key not in _PROMPT_STOPWORDS:
            seen.add(key)
            out.append(term.strip())
    return out


def _hit_matches_terms(title: str, body: str, terms: list[str]) -> bool:
    if not terms:
        return True
    text = f"{title} {body}".lower()
    return any(term.lower() in text for term in terms)


def _hit_matches_titles(title: str, body: str, job_titles: list[str], *, prompt: str = "") -> bool:
    titles = _canonical_job_titles(job_titles, prompt)
    if not titles:
        return True
    head = _serp_title_head(title)
    if _wants_marketing(prompt, job_titles):
        return (
            _has_marketing_cue(head)
            or _has_marketing_cue(_experience_line(body))
            or _has_marketing_cue((body or "")[:280])
        )
    lowered = f"{head} {body}".lower()
    for job in titles:
        if job.lower() in lowered:
            return True
        words = [w for w in re.findall(r"[a-z0-9]+", job.lower()) if w not in _PROMPT_STOPWORDS]
        if words and all(word in lowered for word in words):
            return True
    return False


#: When the user asks for Pakistan (or a Pakistani city), these cues mean the
#: SERP hit is a name collision abroad — NUST Namibia, UMT Montana, CUP India.
_PAKISTAN_COLLISION = re.compile(
    r"(?i)\b(namibia|windhoek|nust\.na|university of montana|\bmso\.umt\.edu\b|"
    r"missoula|central university of punjab|cup\.edu\.in|bathinda|"
    r"greater jaipur|jaunpur)\b"
)


def _hit_matches_location(
    title: str,
    body: str,
    parsed_location: str,
    *,
    location: str,
    prompt: str,
    company: str = "",
) -> bool:
    if not location and not _wants_usa(location, prompt):
        return True
    primary = (body or "").split("...")[0][:220]
    evidence = f"{parsed_location} {company} {_experience_line(body)} {_serp_title_head(title)} {primary}"
    if _wants_usa(location, prompt):
        if _FOREIGN_ONLY.search(evidence):
            return False
        if _has_us_cue(evidence):
            return True
        if re.search(r"\b(Inc\.?|LLC|Ltd\.?|Corp\.?)\b", company or ""):
            return True
        return False
    wanted = [item.lower() for item in _as_list(location)]
    hay = f"{title} {body} {parsed_location} {company}".lower()
    # Pakistan / city searches: reject known foreign lookalikes even if the
    # word "Pakistan" appears elsewhere in a noisy snippet.
    pakistan_wanted = any(
        term in {"pakistan", "lahore", "islamabad", "karachi", "faisalabad",
                 "rawalpindi", "multan", "peshawar", "gujranwala"}
        for term in wanted
    ) or bool(re.search(r"(?i)\bpakistan\b", prompt or ""))
    if pakistan_wanted and _PAKISTAN_COLLISION.search(hay):
        return False
    return any(term in hay for term in wanted)


_US_SEARCH_REGIONS = (
    "California",
    "Texas",
    "Florida",
    "New York",
    "Arizona",
    "Colorado",
    "Illinois",
    "Georgia",
    "North Carolina",
    "Pennsylvania",
    "Massachusetts",
    "Washington",
    "Nevada",
    "Ohio",
)


_US_SEARCH_CITIES = (
    "Austin",
    "Houston",
    "Dallas",
    "Phoenix",
    "Denver",
    "Miami",
    "Atlanta",
    "Chicago",
    "Boston",
    "Seattle",
    "San Diego",
    "Las Vegas",
    "San Francisco",
    "Los Angeles",
    "Philadelphia",
    "Charlotte",
    "Nashville",
    "Tampa",
    "Portland",
    "Minneapolis",
    "Jacksonville",
    "Raleigh",
)


def _core_queries(
    prompt: str,
    *,
    location: str,
    industry: str,
    company: str,
    job_titles: list[str],
) -> list[str]:
    titles = _query_job_titles(job_titles, prompt)
    loc_terms = _location_query_terms(location, prompt)
    sector0 = (_sector_terms(prompt, industry, company) or [""])[0]
    loc_phrase = loc_terms[0] if loc_terms and not _wants_usa(location, prompt) else ""
    queries: list[str] = []
    for title in titles[:3]:
        parts = ["site:linkedin.com/in", f'"{title}"']
        if sector0:
            parts.append(sector0)
        if loc_phrase:
            parts.append(f'"{loc_phrase}"')
        queries.append(" ".join(parts))
    for named in _as_list(company)[:8]:
        for title in titles[:2]:
            queries.append(f'site:linkedin.com/in "{title}" "{named}"')
    return list(dict.fromkeys(queries))


def _expand_queries(
    prompt: str,
    *,
    location: str,
    industry: str,
    company: str,
    job_titles: list[str],
) -> list[str]:
    titles = _query_job_titles(job_titles, prompt)
    loc_terms = _location_query_terms(location, prompt)
    sector0 = (_sector_terms(prompt, industry, company) or [""])[0]
    if not titles:
        return []
    queries: list[str] = []
    if _wants_usa(location, prompt):
        for i, region in enumerate(_US_SEARCH_REGIONS):
            title = titles[i % min(3, len(titles))]
            bit = f"{sector0} {region}".strip()
            queries.append(f'site:linkedin.com/in "{title}" {bit}')
        for i, city in enumerate(_US_SEARCH_CITIES):
            title = titles[i % min(2, len(titles))]
            bit = f"{sector0} {city}".strip()
            queries.append(f'site:linkedin.com/in "{title}" {bit}')
        for title in titles[3:]:
            bit = f"{sector0}".strip()
            queries.append(f'site:linkedin.com/in "{title}" {bit}'.strip())
    for extra_loc in loc_terms:
        if extra_loc.lower() in {"usa", "united states", "us", "u.s.", "u.s.a."}:
            continue
        for title in titles[:2]:
            bit = f"{sector0} {extra_loc}".strip()
            queries.append(f'site:linkedin.com/in "{title}" {bit}')
    return list(dict.fromkeys(queries))


def _build_queries(
    prompt: str,
    *,
    location: str,
    industry: str,
    company: str,
    interests: list[str],
    job_titles: list[str],
    company_size: str,
) -> list[str]:
    return _core_queries(prompt, location=location, industry=industry, company=company, job_titles=job_titles)


_MAX_SEARCH_SECONDS = 100.0


def _query_label(query: str) -> str:
    quoted = re.findall(r'"([^"]+)"', query or "")
    title = quoted[0] if quoted else "marketing contacts"
    loc = next((region for region in _US_SEARCH_REGIONS if region in (query or "")), "")
    if "United States" in (query or ""):
        loc = loc or "the United States"
    if quoted and len(quoted) > 1 and quoted[1] not in {"United States"}:
        return f"People at {quoted[1]}"
    if loc:
        return f"{title} in {loc}"
    return f"Public search for {title}"


def _linkedin_company_from_hit(title: str, url: str) -> str:
    href = (url or "").lower()
    if "linkedin.com/company" not in href:
        return ""
    text = re.sub(r"\s*\|\s*LinkedIn.*$", "", title or "", flags=re.I).strip()
    text = re.split(r"\s+[-–—]\s+", text)[0].strip()
    return _clean_company(text)


def _discover_companies(sector: str, loc_phrase: str, ddgs: Any, *, limit: int = 20) -> list[str]:
    if not sector:
        return []
    loc = loc_phrase or "United States"
    queries = [
        f'site:linkedin.com/company {sector} "{loc}"',
        f'{sector} companies "{loc}" (Inc OR LLC)',
    ]
    found: list[str] = []
    seen: set[str] = set()
    for query in queries:
        for hit in ddg_text(query, max_results=15, ddgs=ddgs, retry=False):
            href = (hit.get("href") or hit.get("link") or "").strip()
            title = hit.get("title") or ""
            body = hit.get("body") or hit.get("snippet") or ""
            name = _linkedin_company_from_hit(title, href)
            if not name:
                name = _clean_company(re.split(r"\s+[-|]\s+", title)[0])
            if not name or not is_usable_company(name):
                continue
            if sector.lower() not in f"{name} {title} {body}".lower():
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(name)
            if len(found) >= limit:
                return found
    return found


def _activity_status(title: str, body: str) -> tuple[str, float]:
    """Return (label, boost) based on public snippet cues — not LinkedIn login data."""
    text = f"{title} {body}".lower()
    hits = sum(1 for cue in _ACTIVE_CUES if cue in text)
    if hits >= 3:
        return "Active", 0.12
    if hits >= 1:
        return "Likely active", 0.06
    return "Unknown activity", 0.0


def _business_associated(title: str, body: str, company: str) -> bool:
    if _clean_company(company):
        return True
    text = f"{title} {body}".lower()
    return any(cue in text for cue in _BUSINESS_CUES)


def _relevance_score(
    *,
    prompt: str,
    title: str,
    body: str,
    role: str,
    company: str,
    location: str,
    job_titles: list[str],
    wants_marketing: bool,
    wants_us: bool,
) -> float:
    text = f"{title} {body} {role} {company} {location}".lower()
    tokens = {
        tok for tok in re.findall(r"[a-z0-9]{3,}", prompt.lower()) if tok not in _PROMPT_STOPWORDS
    }
    hay = set(re.findall(r"[a-z0-9]{3,}", text))
    score = 0.28
    if tokens:
        score += 0.22 * (len(tokens & hay) / max(len(tokens), 1))
    if wants_marketing and _has_marketing_cue(role):
        score += 0.22
    elif wants_marketing:
        score -= 0.2
    if is_usable_company(company):
        score += 0.12
    elif company:
        score -= 0.08
    if wants_us and (_has_us_cue(text) or _has_us_cue(location)):
        score += 0.1
    elif wants_us:
        score -= 0.12
    if "solar" in (company or "").lower():
        score += 0.1
    titles = _canonical_job_titles(job_titles, prompt)
    if role and titles and any(job.lower() in role.lower() for job in titles):
        score += 0.08
    return round(min(0.95, max(0.2, score)), 2)


def _age_band(age_min: int | None, age_max: int | None) -> str:
    amin = age_min if age_min is not None else 18
    amax = age_max if age_max is not None else 65
    return f"{amin}-{amax}"


def search_leads(
    prompt: str,
    *,
    location: str = "",
    industry: str = "",
    company: str = "",
    interests: list[str] | None = None,
    job_titles: list[str] | None = None,
    company_size: str = "",
    income_level: str = "",
    gender: str = "",
    age_min: int | None = None,
    age_max: int | None = None,
    language: str = "",
    max_results: int = 12,
    active_only: bool = True,
    business_only: bool = True,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    interests = _as_list(interests)
    job_titles = _as_list(job_titles)
    companies = _as_list(company)
    industries = _as_list(industry)
    max_results = max(10, min(int(max_results or 2000), 5000))

    if not prompt or not prompt.strip():
        return {"leads": [], "query_used": "", "count": 0, "message": "Enter a marketing prompt."}

    core = _core_queries(prompt, location=location, industry=industry, company=company, job_titles=job_titles)
    extra = _expand_queries(prompt, location=location, industry=industry, company=company, job_titles=job_titles)
    queries = core
    required = _required_terms(prompt, companies=companies, industries=industries)
    wants_marketing = _wants_marketing(prompt, job_titles)
    wants_us = _wants_usa(location, prompt)
    job_titles = _canonical_job_titles(job_titles, prompt)
    sector_terms = _sector_terms(prompt, industry, company)
    loc_phrase = "United States" if wants_us else (_location_query_terms(location, prompt)[:1] or [""])[0]
    query_titles = _query_job_titles(job_titles, prompt)

    leads: list[Lead] = []
    query_used = queries[0] if queries else ""
    seen_urls: set[str] = set()
    engine_hits = 0
    searches_run = 0
    started = time.monotonic()
    deadline = started + _MAX_SEARCH_SECONDS
    age_range = _age_band(age_min, age_max)
    known_industry = next((item for item in industries if item.lower() in _KNOWN_INDUSTRIES), "")
    queued = list(dict.fromkeys([*core, *extra]))
    query_used = queued[0] if queued else ""

    def report(pct: int, stage: str) -> None:
        if on_progress:
            on_progress(max(1, min(88, pct)), stage, len(leads))

    def report_now(stage: str) -> None:
        elapsed = time.monotonic() - started
        pct = 6 + int(min(80, (elapsed / _MAX_SEARCH_SECONDS) * 80))
        report(pct, stage)

    report(4, "Starting a 2-minute public search")

    def consider(hit: dict[str, str]) -> None:
        url = (hit.get("href") or hit.get("link") or "").strip()
        linkedin = _normalize_linkedin_url(url)
        if not linkedin or linkedin in seen_urls:
            return
        title = hit.get("title") or ""
        body = hit.get("body") or hit.get("snippet") or ""
        if not _hit_matches_terms(title, body, required):
            return
        if not _hit_matches_titles(title, body, job_titles, prompt=prompt):
            return
        name, role, company_name = _parse_linkedin_title(title, body)
        if not name:
            return
        if not role:
            role = _role_from_snippet(body)
        if wants_marketing and not (
            _has_marketing_cue(role)
            or _has_marketing_cue(_serp_title_head(title))
            or _has_marketing_cue((body or "")[:280])
        ):
            return
        if business_only and not is_usable_company(company_name):
            return
        if not _matches_sector(
            sector_terms, name=name, company=company_name, role=role, body=body
        ):
            return
        status, boost = _activity_status(title, body)
        associated = _business_associated(title, body, company_name)
        if active_only and status == "Unknown activity":
            if not associated:
                return
            status = "Indexed profile"
        person_location = _location_from_snippet(body)
        if not _hit_matches_location(
            title, body, person_location, location=location, prompt=prompt, company=company_name
        ):
            return
        tags = ["LinkedIn", status]
        tags.extend(t for t in [known_industry, company_size, income_level, gender, language] if t)
        tags.extend(interests[:2])
        if associated:
            tags.append("Business-associated")
        seen_urls.add(linkedin)
        leads.append(
            Lead(
                name=name,
                title=role,
                company=company_name,
                industry=known_industry,
                location=person_location,
                linkedin_url=linkedin,
                website=linkedin,
                email_guess="",
                age_range=age_range,
                relevance_score=round(min(0.96, _relevance_score(
                    prompt=prompt,
                    title=title,
                    body=body,
                    role=role,
                    company=company_name,
                    location=person_location,
                    job_titles=job_titles,
                    wants_marketing=wants_marketing,
                    wants_us=wants_us,
                ) + boost), 2),
                activity_status=status,
                business_associated=associated,
                snippet=(body or title)[:220],
                source=linkedin,
                tags=[t for t in tags if t][:8],
            )
        )

    work: deque[tuple[str, int, bool, int]] = deque()
    seen_queries: set[tuple[str, int]] = set()
    paged: set[str] = set()
    enqueued_employers: set[str] = set()

    def enqueue(q: str, *, page: int = 1, retry: bool = False, per_query: int = 12) -> None:
        key = (q, page)
        if not q or key in seen_queries:
            return
        seen_queries.add(key)
        work.append((q, page, retry, per_query))

    for i, q in enumerate(queued):
        enqueue(q, retry=i == 0, per_query=12)

    def run_query(q: str, *, page: int = 1, retry: bool = False, per_query: int = 12) -> list:
        nonlocal searches_run, engine_hits, query_used
        hits = ddg_text(q, max_results=per_query, page=page, retry=retry)
        searches_run += 1
        report_now(_query_label(q))
        if hits:
            engine_hits += len(hits)
            query_used = query_used or q
        return hits

    def enqueue_employers() -> None:
        if time.monotonic() > deadline - 12 or len(enqueued_employers) >= 10:
            return
        title0 = query_titles[0] if query_titles else "Marketing Manager"
        for lead in leads:
            name = (lead.company or "").strip()
            key = name.lower()
            if not name or key in enqueued_employers or not is_usable_company(name):
                continue
            enqueued_employers.add(key)
            enqueue(f'site:linkedin.com/in "{title0}" "{name}"', retry=False, per_query=8)
            if len(enqueued_employers) >= 10:
                return

    empty_streak = 0
    while work and time.monotonic() < deadline and len(leads) < max_results:
        q, page, retry, per_query = work.popleft()
        hits = run_query(q, page=page, retry=retry, per_query=per_query)
        if hits:
            empty_streak = 0
            if page == 1 and q not in paged and len(hits) >= 8:
                paged.add(q)
                enqueue(q, page=2, retry=False, per_query=10)
        else:
            empty_streak += 1
            if empty_streak >= 3:
                report_now("Search engine cooling down")
                time.sleep(min(6.0, max(2.5, empty_streak)))
            if empty_streak >= 10:
                empty_streak = 4
        before = len(leads)
        for hit in hits:
            consider(hit)
            if len(leads) >= max_results:
                break
        if len(leads) > before:
            enqueue_employers()

    report(88, f"Found {len(leads)} people from {searches_run} searches")

    if not engine_hits and not leads:
        return {
            "leads": [],
            "query_used": query_used,
            "count": 0,
            "message": (
                "Search engine is busy or rate-limited. Wait about 20 seconds and try again "
                "— we did not invent sample leads."
            ),
            "mode": "error",
        }

    leads.sort(key=lambda item: (item.activity_status == "Active", item.relevance_score), reverse=True)
    leads = leads[:max_results]

    if not leads:
        return {
            "leads": [],
            "query_used": query_used,
            "count": 0,
            "message": "No matching LinkedIn people found. Try a simpler prompt or fewer filters.",
            "mode": "live",
        }

    active_n = sum(1 for item in leads if item.activity_status in {"Active", "Likely active"})
    return {
        "leads": [asdict(item) for item in leads],
        "query_used": query_used,
        "count": len(leads),
        "message": (
            f"Found {len(leads)} business people ({active_n} marked active) "
            f"from {searches_run} public searches — no login required."
        ),
        "mode": "live",
    }
