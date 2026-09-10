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

# Snippet cues that a profile looks recently active / engaged on LinkedIn
_ACTIVE_CUES = (
    "posted",
    "shared",
    "activity",
    "commented",
    "published",
    "speaking",
    "speaker",
    "hiring",
    "open to work",
    "actively",
    "recent",
    "2024",
    "2025",
    "2026",
    "followers",
    "connections",
    " co-",
    "founder",
    "ceo",
    "owner",
    "director",
    "manager",
    "head of",
    "vp ",
    "vice president",
)

_BUSINESS_CUES = (
    " at ",
    " @ ",
    "inc",
    "llc",
    "ltd",
    "company",
    "studio",
    "agency",
    "group",
    "solutions",
    "technologies",
    "partners",
    "consulting",
    "founder",
    "ceo",
    "owner",
    "co-founder",
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


def _slug_email(name: str, company: str) -> str:
    first = re.sub(r"[^a-z]", "", name.split()[0].lower()) if name else "contact"
    domain = "example.com"
    if company:
        cleaned = re.sub(r"[^a-z0-9]", "", company.lower().split()[0])
        if cleaned:
            domain = f"{cleaned}.com"
    return f"{first}@{domain}"


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


def _parse_linkedin_title(title: str) -> tuple[str, str, str]:
    """
    Parse common LinkedIn SERP titles:
      'Jane Doe - Founder - Acme Studio | LinkedIn'
      'Jane Doe | Founder at Acme | LinkedIn'
    """
    cleaned = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title, flags=re.I).strip()
    cleaned = re.sub(r"\s*-\s*LinkedIn\s*$", "", cleaned, flags=re.I).strip()

    name, role, company = "", "", ""

    if " - " in cleaned:
        parts = [p.strip() for p in cleaned.split(" - ") if p.strip()]
        if len(parts) >= 1:
            name = parts[0]
        if len(parts) >= 2:
            role = parts[1]
        if len(parts) >= 3:
            company = parts[2]
    elif " | " in cleaned:
        parts = [p.strip() for p in cleaned.split(" | ") if p.strip()]
        if parts:
            name = parts[0]
        if len(parts) >= 2:
            mid = parts[1]
            if " at " in mid.lower():
                left, right = re.split(r"\s+at\s+", mid, maxsplit=1, flags=re.I)
                role, company = left.strip(), right.strip()
            else:
                role = mid
    else:
        name = cleaned[:60]

    # Role may embed company: "Founder at Harbor Co"
    if role and not company and re.search(r"\s+at\s+", role, re.I):
        left, right = re.split(r"\s+at\s+", role, maxsplit=1, flags=re.I)
        role, company = left.strip(), right.strip()

    if not name:
        name = "LinkedIn Member"
    return name, role or "Professional", company or "Business"


def _guess_title(prompt: str, job_titles: list[str]) -> str:
    if job_titles:
        return job_titles[0]
    lowered = prompt.lower()
    mapping = [
        ("founder", "Founder"),
        ("ceo", "CEO"),
        ("marketing", "Marketing Manager"),
        ("owner", "Owner"),
        ("director", "Director"),
        ("manager", "Manager"),
        ("agency", "Agency Lead"),
        ("coach", "Coach"),
        ("dentist", "Practice Owner"),
        ("restaurant", "Owner"),
    ]
    for key, title in mapping:
        if key in lowered:
            return title
    return "Decision Maker"


def _build_queries(
    prompt: str,
    *,
    location: str,
    industry: str,
    interests: list[str],
    job_titles: list[str],
    company_size: str,
    age_min: int | None,
    age_max: int | None,
    active_only: bool,
) -> list[str]:
    parts = [prompt.strip()]
    if industry:
        parts.append(industry)
    if location:
        parts.append(location)
    if company_size:
        parts.append(f"{company_size} employees")
    if interests:
        parts.append(" ".join(interests[:3]))
    if job_titles:
        parts.append(" ".join(job_titles[:2]))
    if age_min is not None or age_max is not None:
        amin = age_min if age_min is not None else 18
        amax = age_max if age_max is not None else 65
        parts.append(f"age {amin}-{amax}")

    base = " ".join(p for p in parts if p)
    active_bits = " active posted activity" if active_only else " active"

    return [
        f'site:linkedin.com/in {base}{active_bits}',
        f'site:linkedin.com/in "{prompt.strip()}" {location or ""} {industry or ""} founder OR owner OR CEO OR manager'.strip(),
        f'site:linkedin.com/in {base} company OR business OR founder OR "at "',
        f'site:linkedin.com/in {job_titles[0] if job_titles else "founder"} {industry or prompt.strip()} {location or ""}',
    ]


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
    if company and company.lower() not in {"business", "unknown", "n/a", "linkedin member"}:
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


def _slug_from_url(url: str) -> str:
    m = re.search(r"/in/([^/?#]+)", url, re.I)
    return m.group(1) if m else ""


def search_leads(
    prompt: str,
    *,
    location: str = "",
    industry: str = "",
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
    interests = interests or []
    job_titles = job_titles or []
    max_results = max(3, min(int(max_results or 12), 25))

    if not prompt or not prompt.strip():
        return {"leads": [], "query_used": "", "count": 0, "message": "Enter a marketing prompt."}

    queries = _build_queries(
        prompt,
        location=location,
        industry=industry,
        interests=interests,
        job_titles=job_titles,
        company_size=company_size,
        age_min=age_min,
        age_max=age_max,
        active_only=active_only,
    )

    raw_hits: list[dict[str, str]] = []
    query_used = queries[0]
    seen_urls: set[str] = set()

    try:
        with DDGS() as ddgs:
            for q in queries:
                for hit in ddgs.text(q, max_results=max_results + 8):
                    url = (hit.get("href") or hit.get("link") or "").strip()
                    linkedin = _normalize_linkedin_url(url)
                    if not linkedin or linkedin in seen_urls:
                        continue
                    seen_urls.add(linkedin)
                    raw_hits.append(
                        {
                            "title": hit.get("title") or "",
                            "body": hit.get("body") or hit.get("snippet") or "",
                            "href": linkedin,
                        }
                    )
                    if len(raw_hits) >= max_results * 3:
                        break
                if len(raw_hits) >= max_results * 2:
                    break
                query_used = q
    except Exception as exc:  # network / rate-limit
        return {
            "leads": _fallback_leads(
                prompt,
                location=location,
                industry=industry,
                interests=interests,
                job_titles=job_titles,
                company_size=company_size,
                income_level=income_level,
                age_min=age_min,
                age_max=age_max,
                max_results=max_results,
                active_only=active_only,
            ),
            "query_used": query_used,
            "count": max_results,
            "message": f"Live LinkedIn search unavailable ({exc}); showing sample LinkedIn-style leads.",
            "mode": "sample",
        }

    leads: list[Lead] = []
    age_range = _age_band(age_min, age_max)
    default_title = _guess_title(prompt, job_titles)

    for hit in raw_hits:
        name, role, company = _parse_linkedin_title(hit["title"])
        if role == "Professional" and default_title:
            role = default_title

        status, boost = _activity_status(hit["title"], hit["body"])
        if active_only and status == "Unknown activity":
            # Still keep if title shows a clear business role (public profiles indexed as people@co)
            if not _business_associated(hit["title"], hit["body"], company):
                continue
            status = "Indexed profile"

        associated = _business_associated(hit["title"], hit["body"], company)
        if business_only and not associated:
            continue

        tags = ["LinkedIn", status]
        tags.extend(t for t in [industry, company_size, income_level, gender, language] if t)
        tags.extend(interests[:2])
        if location:
            tags.append(location)
        if associated:
            tags.append("Business-associated")

        text_blob = f"{hit['title']} {hit['body']} {prompt}"
        lead = Lead(
            name=name,
            title=role,
            company=company,
            industry=industry or "General",
            location=location or "Not specified",
            linkedin_url=hit["href"],
            website=hit["href"],
            email_guess=_slug_email(name, company),
            age_range=age_range,
            relevance_score=_stable_score(text_blob, prompt, activity_boost=boost, business=associated),
            activity_status=status,
            business_associated=associated,
            snippet=(hit["body"] or hit["title"])[:220],
            source=hit["href"],
            tags=[t for t in tags if t][:8],
        )
        leads.append(lead)

    leads.sort(key=lambda L: (L.activity_status == "Active", L.relevance_score), reverse=True)
    leads = leads[:max_results]

    if not leads:
        sample = _fallback_leads(
            prompt,
            location=location,
            industry=industry,
            interests=interests,
            job_titles=job_titles,
            company_size=company_size,
            income_level=income_level,
            age_min=age_min,
            age_max=age_max,
            max_results=max_results,
            active_only=active_only,
        )
        return {
            "leads": sample,
            "query_used": query_used,
            "count": len(sample),
            "message": "No LinkedIn people matches found; showing sample active business profiles.",
            "mode": "sample",
        }

    active_n = sum(1 for L in leads if L.activity_status in {"Active", "Likely active"})
    return {
        "leads": [asdict(L) for L in leads],
        "query_used": query_used,
        "count": len(leads),
        "message": f"Found {len(leads)} business people ({active_n} marked active) via public search — no login required.",
        "mode": "live",
    }


def _fallback_leads(
    prompt: str,
    *,
    location: str,
    industry: str,
    interests: list[str],
    job_titles: list[str],
    company_size: str,
    income_level: str,
    age_min: int | None,
    age_max: int | None,
    max_results: int,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """Deterministic sample LinkedIn-style leads."""
    seeds = [
        ("Ava Chen", "Brightline Studio", "Founder"),
        ("Marcus Webb", "Northpeak Media", "CEO"),
        ("Sofia Alvarez", "Harbor & Co", "Marketing Manager"),
        ("Jonah Pierce", "Velvet Metric", "Owner"),
        ("Priya Nair", "Orbit Collective", "Growth Lead"),
        ("Leo Hartmann", "Cedar Growth", "Director"),
        ("Maya Okonkwo", "Lumen Field", "CMO"),
        ("Ethan Brooks", "Pulse & Partner", "Agency Owner"),
        ("Clara Nguyen", "Second Street", "Brand Manager"),
        ("Diego Morales", "Atlas Reach", "Founder"),
        ("Nora Feldman", "Kindling Labs", "Head of Marketing"),
        ("Samir Patel", "Copperline", "Owner"),
    ]
    title_default = _guess_title(prompt, job_titles)
    age_range = _age_band(age_min, age_max)
    ind = industry or "Marketing & Advertising"
    loc = location or "Remote / Multi-market"
    size = company_size or "11-50"
    leads: list[dict[str, Any]] = []

    for i, (name, company, seed_title) in enumerate(seeds[:max_results]):
        title = job_titles[0] if job_titles else seed_title or title_default
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        linkedin = f"https://www.linkedin.com/in/{slug}"
        tags = ["LinkedIn", "Active", "Business-associated", ind, size]
        if income_level:
            tags.append(income_level)
        tags.extend(interests[:2])
        score = round(0.94 - i * 0.03, 2)
        leads.append(
            asdict(
                Lead(
                    name=name,
                    title=title,
                    company=company,
                    industry=ind,
                    location=loc,
                    linkedin_url=linkedin,
                    website=linkedin,
                    email_guess=_slug_email(name, company),
                    age_range=age_range,
                    relevance_score=max(0.55, score),
                    activity_status="Active" if active_only or i % 2 == 0 else "Likely active",
                    business_associated=True,
                    snippet=f"Active LinkedIn profile associated with {company}. Matches brief “{prompt[:70]}”.",
                    source=linkedin,
                    tags=tags[:8],
                )
            )
        )
    return leads
