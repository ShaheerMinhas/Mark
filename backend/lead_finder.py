"""Marketing lead discovery — LinkedIn people at businesses (via open-web search)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS  # type: ignore


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
}

# Common false positives after the word "at"
_AT_STOPWORDS = {
    "the", "a", "an", "his", "her", "their", "our", "my", "linkedin", "least",
    "home", "work", "scale", "ease", "heart", "hand", "night", "university",
    "college", "school", "least", "present", "this", "that", "which",
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
}

# High-confidence job titles used before "at <Company>"
_JOB_TITLE_RE = (
    r"(?:Co-?Founder|Founder|CEO|CTO|CMO|CFO|COO|Owner|President|Director|"
    r"Managing Director|General Manager|"
    r"(?:Principal |Principle |Senior |Staff |Lead |Junior )?"
    r"(?:Software |Data |Platform |DevOps |IT |Enterprise )?"
    r"(?:Engineer|Developer|Designer|Consultant|Analyst|Architect|Specialist|Manager)|"
    r"Marketing Manager|Brand Manager|Product Manager|Sales Manager|Project Manager|"
    r"Growth Lead|Head of [A-Za-z& ]{2,30}|"
    r"VP(?:[\s\-–]+(?:of\s+)?[A-Za-z][\w&.''\-]*(?:\s+[A-Za-z][\w&.''\-]*){0,6})?|"
    r"Vice President(?:\s+of)?(?:\s+[A-Za-z&][A-Za-z& ]{1,30})?|"
    r"Partner|Principal|Lead)"
)

_KNOWN_INDUSTRIES = {item.lower() for item in INDUSTRIES}

_SERP_JUNK = re.compile(
    r"(?i)(?:wikipedia|yahoo|sign in|log\s?in|facebook|meta for business|"
    r"wireless phone|total commander|practo|at sign|linkedinat)"
)
_PERSON_NAME_RE = re.compile(
    r"^("
    r"[A-Z][a-zA-Z'’\-]+"
    r"(?:\s+[A-Z]\.)?"
    r"(?:\s+[A-Z][a-zA-Z'’\-]+){0,3}"
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


def _looks_like_location(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
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
    if re.search(r"linkedin|wikipedia|yahoo|http|profile|followers|connections", lowered):
        return ""
    if re.search(
        r"(?i)\b(engineer|manager|director|founder|consultant|president|software|intern|student)\b",
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
    cleaned = re.sub(r"\s*\|\s*LinkedIn.*$", "", cleaned, flags=re.I)
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
    if "..." in company:
        company = company.split("...")[0].strip()

    if _EMAIL_IN_TEXT.fullmatch(company):
        return ""
    if re.search(r"\b(view|profile|linkedin|connections?|followers?|see the complete|wikipedia)\b", company, re.I):
        return ""
    if _looks_like_location(company):
        return ""
    if len(company) < 2 or len(company) > 80:
        return ""
    lower = company.lower()
    if lower in _GENERIC_COMPANIES:
        return ""
    if name and lower == name.lower():
        return ""
    first = re.split(r"\s+", lower)[0]
    if first in _AT_STOPWORDS:
        return ""
    words = re.findall(r"[a-z0-9]+", lower)
    if words and all(w in _ROLE_WORDS for w in words):
        return ""
    if re.search(r"\b(who|which|where|that|with|from|about|looking|based)\b", lower):
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


def _company_from_snippet(body: str, *, name: str = "") -> str:
    """Conservative snippet scrape — only high-confidence employer patterns."""
    if not body:
        return ""
    patterns = [
        rf"(?i)Experience[:\s]+{_JOB_TITLE_RE}\s+(?:at|@)\s*([^·|\n]+)",
        rf"(?i)\b{_JOB_TITLE_RE}\s+(?:at|@)\s*([^·|\n]+)",
        r"([A-Z][\w&.''\-]*(?:\s+(?:&|and|[A-Z][\w&.''\-]*)){0,4})\s*[·|]\s*(?:Full-?time|Part-?time|Contract)\b",
    ]
    for pat in patterns:
        match = re.search(pat, body)
        if match:
            cleaned = _clean_company(match.group(1), name=name)
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
    match = re.search(r"(?i)Location:\s*([^·|\n]{2,50})", body or "")
    if not match:
        return ""
    loc = match.group(1).strip(" .")
    loc = re.split(r"\s{2,}", loc)[0].strip()
    return loc if 2 < len(loc) < 60 else ""


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
    role, company = "", ""
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
            left, right = _split_comma_company(rest)
            if right:
                role, company = left, right
            elif _looks_like_role(rest):
                role = rest
            else:
                maybe_company = _clean_company(rest, name=name)
                if maybe_company:
                    company = maybe_company
                else:
                    role = rest

    role = _clean_role(role)
    company = _clean_company(company, name=name)
    if not company and body:
        company = _company_from_snippet(body, name=name)
    if not role and body:
        role = _role_from_snippet(body)
    return name, role, company


def _required_terms(prompt: str, *, companies: list[str], industries: list[str]) -> list[str]:
    terms: list[str] = []
    terms.extend(companies)
    for industry in industries:
        if industry.lower() not in _KNOWN_INDUSTRIES:
            terms.append(industry)
    _role, prompt_company = _split_role_at_company(prompt)
    if prompt_company:
        terms.append(" ".join(prompt_company.split()[:3]))
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


def _hit_matches_titles(title: str, body: str, job_titles: list[str]) -> bool:
    titles = [item for item in job_titles if item]
    if not titles:
        return True
    text = f"{title} {body}".lower()
    for job in titles:
        if job.lower() in text:
            return True
        words = [w for w in re.findall(r"[a-z0-9]+", job.lower()) if w not in _PROMPT_STOPWORDS]
        if words and all(word in text for word in words):
            return True
    return False


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
    locations = _as_list(location)
    industries = _as_list(industry)
    companies = _as_list(company)
    known = [item for item in industries if item.lower() in _KNOWN_INDUSTRIES]
    extra = [item for item in industries if item.lower() not in _KNOWN_INDUSTRIES] + companies

    loc = _or_clause(locations)
    ind = _or_clause(known)
    firms = _or_clause(extra)
    jobs = _or_clause(job_titles)
    size = f'"{company_size} employees"' if company_size else ""
    interest = _or_clause(interests[:3])
    brief = prompt.strip()

    queries: list[str] = []
    primary = " ".join(part for part in ["site:linkedin.com/in", brief, jobs, firms, loc, ind] if part)
    queries.append(primary)
    if jobs or firms:
        queries.append(" ".join(part for part in ["site:linkedin.com/in", jobs or brief, firms, loc, ind] if part))
    if loc and (firms or jobs):
        queries.append(" ".join(part for part in ["site:linkedin.com/in", firms or jobs, loc, size, interest] if part))

    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))


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


def _stable_score(text: str, prompt: str, activity_boost: float = 0.0, business: bool = False) -> float:
    tokens = set(re.findall(r"[a-z0-9]+", prompt.lower()))
    hay = set(re.findall(r"[a-z0-9]+", text.lower()))
    if not tokens:
        base = 0.55
    else:
        overlap = len(tokens & hay) / max(len(tokens), 1)
        salt = int(hashlib.md5(text.encode()).hexdigest()[:4], 16) / 65535.0
        base = 0.35 + overlap * 0.5 + salt * 0.08
    if business:
        base += 0.08
    return round(min(0.98, base + activity_boost), 2)


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
) -> dict[str, Any]:
    interests = _as_list(interests)
    job_titles = _as_list(job_titles)
    companies = _as_list(company)
    industries = _as_list(industry)
    max_results = max(3, min(int(max_results or 12), 100))

    if not prompt or not prompt.strip():
        return {"leads": [], "query_used": "", "count": 0, "message": "Enter a marketing prompt."}

    queries = _build_queries(
        prompt,
        location=location,
        industry=industry,
        company=company,
        interests=interests,
        job_titles=job_titles,
        company_size=company_size,
    )
    required = _required_terms(prompt, companies=companies, industries=industries)

    raw_hits: list[dict[str, str]] = []
    query_used = queries[0] if queries else ""
    seen_urls: set[str] = set()

    try:
        with DDGS() as ddgs:
            for q in queries:
                query_used = q
                for hit in ddgs.text(q, max_results=max_results + 8):
                    url = (hit.get("href") or hit.get("link") or "").strip()
                    linkedin = _normalize_linkedin_url(url)
                    if not linkedin or linkedin in seen_urls:
                        continue
                    title = hit.get("title") or ""
                    body = hit.get("body") or hit.get("snippet") or ""
                    if not _hit_matches_terms(title, body, required):
                        continue
                    if not _hit_matches_titles(title, body, job_titles):
                        continue
                    seen_urls.add(linkedin)
                    raw_hits.append({"title": title, "body": body, "href": linkedin})
                    if len(raw_hits) >= max_results * 3:
                        break
                if len(raw_hits) >= max_results * 2:
                    break
    except Exception as exc:  # network / rate-limit
        return {
            "leads": [],
            "query_used": query_used,
            "count": 0,
            "message": f"Live search failed ({exc}). Try again in a moment — no sample leads were added.",
            "mode": "error",
        }

    leads: list[Lead] = []
    age_range = _age_band(age_min, age_max)
    known_industry = next((item for item in industries if item.lower() in _KNOWN_INDUSTRIES), "")

    for hit in raw_hits:
        name, role, company_name = _parse_linkedin_title(hit["title"], hit["body"])
        if not name:
            continue

        status, boost = _activity_status(hit["title"], hit["body"])
        associated = _business_associated(hit["title"], hit["body"], company_name)
        if active_only and status == "Unknown activity":
            if not associated:
                continue
            status = "Indexed profile"
        if business_only and not associated:
            continue

        tags = ["LinkedIn", status]
        tags.extend(t for t in [known_industry, company_size, income_level, gender, language] if t)
        tags.extend(interests[:2])
        if associated:
            tags.append("Business-associated")

        person_location = _location_from_snippet(hit["body"])
        text_blob = f"{hit['title']} {hit['body']} {prompt}"
        leads.append(
            Lead(
                name=name,
                title=role,
                company=company_name,
                industry=known_industry,
                location=person_location,
                linkedin_url=hit["href"],
                website=hit["href"],
                email_guess="",
                age_range=age_range,
                relevance_score=_stable_score(text_blob, prompt, activity_boost=boost, business=associated),
                activity_status=status,
                business_associated=associated,
                snippet=(hit["body"] or hit["title"])[:220],
                source=hit["href"],
                tags=[t for t in tags if t][:8],
            )
        )

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
        "message": f"Found {len(leads)} business people ({active_n} marked active) via public search — no login required.",
        "mode": "live",
    }
