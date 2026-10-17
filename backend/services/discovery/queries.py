"""Query plan generation.

Builds a bounded, deduplicated set of public-search queries from the user's prompt
and filters by varying one dimension at a time (job title, title synonym,
seniority, industry term, geography, named company, prompt keyword). Varying a
single dimension per query keeps the set diverse; a shared budget plus hashing
keeps it from degenerating into hundreds of near-identical searches.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from backend.lead_finder import (
    _as_list,
    _canonical_job_titles,
    _location_query_terms,
    _query_job_titles,
    _sector_terms,
    _US_SEARCH_CITIES,
    _US_SEARCH_REGIONS,
    _wants_usa,
)

PROFILE_SITE = "site:linkedin.com/in"

#: Default ceiling on queries per search. With 12-wide parallel clients this is
#: the main lever on discovery breadth. ~1000 qualified leads need a few thousand
#: candidates, which needs this many distinct searches; results stream to the UI
#: as they land, so the longer run is not a blocking wait.
DEFAULT_MAX_QUERIES = 180

#: Seniority variants, used to widen a role without changing its function.
SENIORITY_PREFIXES = (
    "Head of",
    "VP",
    "Director of",
    "Senior",
    "Lead",
    "Chief",
    "Global Head of",
)

#: Suffix forms that express the same role differently on LinkedIn.
ROLE_SUFFIXES = ("Manager", "Director", "Lead", "Specialist", "Head")

#: Related vocabulary per sector. The industry dimension previously only fired
#: when the user typed several industries themselves, so a single-industry search
#: never explored the words the target companies actually describe themselves with.
INDUSTRY_TERMS: dict[str, tuple[str, ...]] = {
    "logistics": ("freight", "shipping", "supply chain", "3PL", "transport", "warehousing", "distribution"),
    "freight": ("logistics", "shipping", "forwarding", "haulage", "supply chain"),
    "supply chain": ("logistics", "procurement", "sourcing", "operations"),
    "software": ("SaaS", "technology", "platform", "cloud", "IT"),
    "saas": ("software", "cloud", "platform", "B2B software"),
    "technology": ("software", "SaaS", "IT", "digital"),
    "fintech": ("finance", "payments", "banking", "financial services"),
    "finance": ("fintech", "banking", "investment", "financial services"),
    "banking": ("finance", "financial services", "fintech"),
    "insurance": ("insurtech", "underwriting", "financial services"),
    "healthcare": ("medical", "health", "clinical", "hospital", "life sciences"),
    "pharma": ("pharmaceutical", "biotech", "life sciences", "medical"),
    "biotech": ("life sciences", "pharmaceutical", "research"),
    "manufacturing": ("industrial", "production", "factory", "engineering", "OEM"),
    "industrial": ("manufacturing", "engineering", "machinery", "production"),
    "automotive": ("vehicles", "mobility", "OEM", "automotive supplier"),
    "retail": ("ecommerce", "consumer goods", "FMCG", "merchandising"),
    "ecommerce": ("retail", "online retail", "D2C", "marketplace"),
    "fmcg": ("consumer goods", "retail", "food and beverage"),
    "food": ("beverage", "FMCG", "food production", "hospitality"),
    "construction": ("building", "contracting", "infrastructure", "engineering"),
    "real estate": ("property", "construction", "facilities"),
    "energy": ("renewable", "utilities", "power", "oil and gas", "solar"),
    "renewable": ("energy", "solar", "wind", "cleantech", "clean energy"),
    "solar": (
        "solar energy",
        "photovoltaic",
        "PV",
        "renewable",
        "cleantech",
        "clean energy",
        "solar power",
        "solar panel",
        "inverter",
        "EPCs",
    ),
    "solar energy": (
        "solar",
        "photovoltaic",
        "PV",
        "renewable",
        "cleantech",
        "solar power",
        "solar panel",
    ),
    "photovoltaic": ("solar", "PV", "solar energy", "renewable"),
    "cleantech": ("clean energy", "renewable", "solar", "wind", "climate tech"),
    "clean energy": ("renewable", "solar", "wind", "cleantech", "green energy"),
    "telecom": ("telecommunications", "networking", "connectivity", "mobile"),
    "education": ("edtech", "training", "learning", "university"),
    "marketing": ("advertising", "media", "digital agency", "communications"),
    "advertising": ("marketing", "media", "agency", "brand"),
    "consulting": ("advisory", "professional services", "strategy"),
    "legal": ("law firm", "legal services", "compliance"),
    "recruitment": ("staffing", "talent acquisition", "HR services"),
    "hospitality": ("hotel", "travel", "tourism", "restaurant"),
    "gaming": ("games", "esports", "interactive entertainment"),
    "agriculture": ("agritech", "farming", "food production"),
    "mining": ("metals", "minerals", "resources"),
    "aerospace": ("aviation", "defence", "space"),
    "maritime": ("shipping", "ports", "marine", "offshore"),
}

#: Major business cities per country, so a country-level search can fan out. The
#: US is handled separately by ``_US_SEARCH_REGIONS``.
#: US aliases that must expand to states/cities. ``_location_query_terms`` collapses
#: these to ``USA`` / ``United States``, so without this list a USA search never
#: fans out the way Sweden → Stockholm does.
_US_LOCATION_ALIASES = frozenset(
    {
        "united states",
        "united states of america",
        "usa",
        "u.s.",
        "u.s.a.",
        "u.s.a",
        "us",
        "america",
        "north america",
    }
)

COUNTRY_CITIES: dict[str, tuple[str, ...]] = {
    # States first (LinkedIn indexes them well), then major metros.
    "united states": tuple(
        dict.fromkeys((*_US_SEARCH_REGIONS, *_US_SEARCH_CITIES))
    ),
    "sweden": ("Stockholm", "Gothenburg", "Malmö", "Uppsala", "Linköping"),
    "norway": ("Oslo", "Bergen", "Trondheim", "Stavanger"),
    "denmark": ("Copenhagen", "Aarhus", "Odense", "Aalborg"),
    "finland": ("Helsinki", "Espoo", "Tampere", "Turku"),
    "iceland": ("Reykjavik",),
    "germany": ("Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne", "Stuttgart", "Düsseldorf"),
    "united kingdom": ("London", "Manchester", "Birmingham", "Leeds", "Edinburgh", "Bristol", "Glasgow"),
    "england": ("London", "Manchester", "Birmingham", "Leeds", "Bristol"),
    "scotland": ("Edinburgh", "Glasgow", "Aberdeen"),
    "ireland": ("Dublin", "Cork", "Galway"),
    "netherlands": ("Amsterdam", "Rotterdam", "The Hague", "Utrecht", "Eindhoven"),
    "belgium": ("Brussels", "Antwerp", "Ghent", "Leuven"),
    "france": ("Paris", "Lyon", "Marseille", "Toulouse", "Lille", "Bordeaux"),
    "spain": ("Madrid", "Barcelona", "Valencia", "Seville", "Bilbao"),
    "portugal": ("Lisbon", "Porto"),
    "italy": ("Milan", "Rome", "Turin", "Bologna", "Naples"),
    "switzerland": ("Zurich", "Geneva", "Basel", "Bern", "Lausanne"),
    "austria": ("Vienna", "Graz", "Linz", "Salzburg"),
    "poland": ("Warsaw", "Krakow", "Wroclaw", "Poznan", "Gdansk"),
    "czechia": ("Prague", "Brno", "Ostrava"),
    "romania": ("Bucharest", "Cluj-Napoca", "Timisoara"),
    "hungary": ("Budapest", "Debrecen"),
    "greece": ("Athens", "Thessaloniki"),
    "turkey": ("Istanbul", "Ankara", "Izmir"),
    "canada": ("Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa"),
    "mexico": ("Mexico City", "Monterrey", "Guadalajara"),
    "brazil": ("São Paulo", "Rio de Janeiro", "Belo Horizonte", "Curitiba"),
    "australia": ("Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide"),
    "new zealand": ("Auckland", "Wellington", "Christchurch"),
    "india": ("Bangalore", "Mumbai", "Delhi", "Hyderabad", "Pune", "Chennai", "Gurgaon"),
    "pakistan": ("Karachi", "Lahore", "Islamabad"),
    "singapore": ("Singapore",),
    "malaysia": ("Kuala Lumpur", "Penang", "Johor Bahru"),
    "indonesia": ("Jakarta", "Surabaya", "Bandung"),
    "japan": ("Tokyo", "Osaka", "Nagoya", "Yokohama"),
    "china": ("Shanghai", "Beijing", "Shenzhen", "Guangzhou", "Hangzhou"),
    "south korea": ("Seoul", "Busan"),
    "israel": ("Tel Aviv", "Jerusalem", "Haifa"),
    "united arab emirates": ("Dubai", "Abu Dhabi", "Sharjah"),
    "uae": ("Dubai", "Abu Dhabi"),
    "saudi arabia": ("Riyadh", "Jeddah", "Dammam"),
    "south africa": ("Johannesburg", "Cape Town", "Durban", "Pretoria"),
    "nigeria": ("Lagos", "Abuja"),
    "kenya": ("Nairobi", "Mombasa"),
    "egypt": ("Cairo", "Alexandria"),
}

#: When the user names no geography, fan out across major business markets so a
#: single-industry search is not stuck on a couple of dozen title variants.
GLOBAL_MARKETS: tuple[str, ...] = (
    "United States",
    "California",
    "Texas",
    "New York",
    "Germany",
    "United Kingdom",
    "Netherlands",
    "India",
    "Australia",
    "Canada",
    "France",
    "Spain",
    "UAE",
    "Singapore",
    "Sweden",
    "Brazil",
    "Japan",
    "China",
)

#: Demonyms the prompt may use instead of the country name ("Swedish logistics").
_DEMONYMS: dict[str, str] = {
    "swedish": "sweden", "norwegian": "norway", "danish": "denmark",
    "finnish": "finland", "icelandic": "iceland", "german": "germany",
    "british": "united kingdom", "english": "england", "scottish": "scotland",
    "irish": "ireland", "dutch": "netherlands", "belgian": "belgium",
    "french": "france", "spanish": "spain", "portuguese": "portugal",
    "italian": "italy", "swiss": "switzerland", "austrian": "austria",
    "polish": "poland", "czech": "czechia", "romanian": "romania",
    "hungarian": "hungary", "greek": "greece", "turkish": "turkey",
    "american": "united states",
    "canadian": "canada", "mexican": "mexico", "brazilian": "brazil",
    "australian": "australia", "indian": "india", "pakistani": "pakistan",
    "singaporean": "singapore", "malaysian": "malaysia", "indonesian": "indonesia",
    "japanese": "japan", "chinese": "china", "korean": "south korea",
    "israeli": "israel", "emirati": "united arab emirates",
    "saudi": "saudi arabia", "nigerian": "nigeria", "kenyan": "kenya",
    "egyptian": "egypt",
}


def expand_industry(term: str) -> list[str]:
    """Related sector vocabulary for one industry, or [] when unknown.

    Longer keys win: ``solar energy`` must not collapse to the generic ``energy``
    bag, or solar searches lose their specialist vocabulary.
    """
    key = re.sub(r"\s+", " ", (term or "").strip().lower())
    if not key:
        return []
    if key in INDUSTRY_TERMS:
        return list(INDUSTRY_TERMS[key])
    # Prefer the longest contained industry key ("solar energy" over "energy").
    matches = [
        known
        for known in INDUSTRY_TERMS
        if re.search(rf"\b{re.escape(known)}\b", key)
    ]
    if not matches:
        return []
    known = max(matches, key=len)
    return list(INDUSTRY_TERMS[known])


def _resolve_country(term: str, prompt: str = "") -> str:
    """Map a location string (or a demonym in the prompt) to a COUNTRY_CITIES key."""
    key = re.sub(r"\s+", " ", (term or "").strip().lower()).strip(" ,.")
    if key in _US_LOCATION_ALIASES:
        return "united states"
    if key in COUNTRY_CITIES:
        return key
    if key in _DEMONYMS:
        return _DEMONYMS[key]
    # "United States of America" / multi-word aliases after stripping punctuation.
    for alias in sorted(_US_LOCATION_ALIASES, key=len, reverse=True):
        if alias in key:
            return "united states"
    for word in _WORD.findall(prompt or ""):
        lowered = word.lower()
        if lowered in _US_LOCATION_ALIASES:
            return "united states"
        mapped = _DEMONYMS.get(lowered) or (
            lowered if lowered in COUNTRY_CITIES else ""
        )
        if mapped:
            return mapped
    return ""


def expand_geography(term: str, prompt: str = "") -> list[str]:
    """Major cities/states for a country-level location, or [] when unknown."""
    key = re.sub(r"\s+", " ", (term or "").strip().lower()).strip(" ,.")
    country = _resolve_country(term, prompt)
    if not country:
        return []
    cities = COUNTRY_CITIES.get(country, ())
    # Drop the place the user already asked for.
    return [city for city in cities if city.lower() != key]

_WORD = re.compile(r"[A-Za-z][A-Za-z&/+-]{2,}")
_PROMPT_STOPWORDS = {
    "and", "the", "for", "who", "are", "with", "that", "this", "from", "find",
    "list", "people", "person", "companies", "company", "contacts", "leads",
    "working", "work", "based", "looking", "want", "need", "all", "any", "top",
    "small", "medium", "large", "active", "online", "near", "around", "their",
    "our", "get", "give", "show", "more", "than", "into", "over", "about",
}


@dataclass(frozen=True)
class QuerySpec:
    text: str
    dimension: str
    page: int = 1
    priority: int = 0

    @property
    def hash(self) -> str:
        normalized = re.sub(r"\s+", " ", self.text.strip().lower())
        return hashlib.sha256(f"{normalized}|{self.page}".encode()).hexdigest()[:32]


def _quote(value: str) -> str:
    cleaned = re.sub(r'["\\]', "", (value or "").strip())
    return f'"{cleaned}"' if cleaned else ""


def _compose(*parts: str) -> str:
    return " ".join(part for part in (PROFILE_SITE, *parts) if part).strip()


def _title_synonyms(title: str) -> list[str]:
    """Expand a role into recognisable variants without inventing new functions."""
    base = (title or "").strip()
    if not base:
        return []
    out: list[str] = []
    stem = base
    for suffix in ROLE_SUFFIXES:
        stem = re.sub(rf"\s*\b{suffix}\b\s*$", "", stem, flags=re.I).strip()
    if not stem:
        return []
    for suffix in ROLE_SUFFIXES:
        candidate = f"{stem} {suffix}".strip()
        if candidate.lower() != base.lower():
            out.append(candidate)
    for prefix in SENIORITY_PREFIXES:
        candidate = f"{prefix} {stem}".strip()
        if candidate.lower() != base.lower():
            out.append(candidate)
    return out


def _stem(word: str) -> str:
    """Crude singular form, so "managers" counts as covered by "Manager"."""
    lowered = word.lower()
    for suffix in ("ies", "es", "s"):
        if len(lowered) > len(suffix) + 2 and lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def prompt_keywords(prompt: str, *, exclude: Iterable[str] = ()) -> list[str]:
    """Content words from the prompt not already covered by a structured filter."""
    covered = {_stem(token) for item in exclude for token in _WORD.findall(item or "")}
    out: list[str] = []
    seen: set[str] = set()
    for word in _WORD.findall(prompt or ""):
        lowered = word.lower()
        stem = _stem(word)
        # Stopwords are listed in their natural form, so check the word as written
        # as well as its stem.
        if lowered in _PROMPT_STOPWORDS or stem in _PROMPT_STOPWORDS:
            continue
        if stem in covered or stem in seen or len(stem) < 4:
            continue
        seen.add(stem)
        out.append(word)
    return out


def _extend_unique(base: Sequence[str], extra: Iterable[str]) -> list[str]:
    """``base`` plus any of ``extra`` not already present, case-insensitively."""
    out = list(base)
    seen = {item.strip().lower() for item in out}
    for item in extra:
        key = (item or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _dedupe(specs: Sequence[QuerySpec], limit: int) -> list[QuerySpec]:
    out: list[QuerySpec] = []
    seen: set[str] = set()
    ordered = sorted(enumerate(specs), key=lambda pair: (pair[1].priority, pair[0]))
    for _, spec in ordered:
        if not spec.text or spec.text == PROFILE_SITE:
            continue
        if spec.hash in seen:
            continue
        seen.add(spec.hash)
        out.append(spec)
        if len(out) >= limit:
            break
    return out


def build_query_plan(
    prompt: str,
    filters: dict[str, Any] | None = None,
    *,
    max_queries: int = DEFAULT_MAX_QUERIES,
) -> list[QuerySpec]:
    """Return the ordered, deduplicated queries to run for this search."""
    f = filters or {}
    location = str(f.get("location") or "")
    industry = str(f.get("industry") or "")
    company = str(f.get("company") or "")
    job_titles = _as_list(f.get("job_titles"))

    titles = _query_job_titles(job_titles, prompt) or _canonical_job_titles(job_titles, prompt)
    sectors = [s for s in _sector_terms(prompt, industry, company) if s]
    locations = [loc for loc in _location_query_terms(location, prompt) if loc]
    companies = _as_list(company)
    keywords = prompt_keywords(prompt, exclude=[*titles, *sectors, *locations, *companies])

    primary_sector = sectors[0] if sectors else ""
    primary_location = locations[0] if locations else ""

    # Widen the sector and geography lists before building the dimensions below.
    # Both dimensions slice from index 1 onward, so without this a search naming a
    # single industry and a single country produces title variations only.
    #
    # Expand from the *raw* location filter, not the collapsed ``USA`` /
    # ``United States`` tokens — otherwise "United States of America" never
    # fans out into California / Texas / Austin the way Sweden → Stockholm does.
    sectors = _extend_unique(sectors, expand_industry(primary_sector or industry))
    locations = _extend_unique(
        locations,
        expand_geography(location or primary_location, prompt),
    )
    # Industry-only searches used to stop after ~20 title/sector variants. Fan out
    # across major markets so the candidate pool can actually reach hundreds.
    if not locations and not location.strip():
        locations = list(GLOBAL_MARKETS)
    specs: list[QuerySpec] = []

    # Dimension 1 — the requested titles against the primary sector/geography.
    for title in titles[:6]:
        specs.append(
            QuerySpec(
                _compose(_quote(title), primary_sector, _quote(primary_location)),
                "title",
                priority=0,
            )
        )

    # Dimension 2 — title synonyms and seniority variants.
    for title in titles[:4]:
        for synonym in _title_synonyms(title)[:8]:
            specs.append(
                QuerySpec(
                    _compose(_quote(synonym), primary_sector, _quote(primary_location)),
                    "title_synonym",
                    priority=1,
                )
            )

    # Dimension 3 — alternative industry/sector terminology.
    for sector in sectors[1:12]:
        for title in titles[:4]:
            specs.append(
                QuerySpec(
                    _compose(_quote(title), sector, _quote(primary_location)),
                    "industry_term",
                    priority=2,
                )
            )

    # Dimension 4 — additional geographies, named by the user, expanded from a
    # country into its cities, or the global market sweep above.
    for loc in locations[:16]:
        if loc.lower() == (primary_location or "").lower():
            continue
        for title in titles[:3]:
            specs.append(
                QuerySpec(
                    _compose(_quote(title), primary_sector, _quote(loc)),
                    "geography",
                    priority=2,
                )
            )
        # Cross a couple of industry synonyms into each market for volume.
        for sector in sectors[1:4]:
            for title in titles[:2]:
                specs.append(
                    QuerySpec(
                        _compose(_quote(title), sector, _quote(loc)),
                        "geography",
                        priority=3,
                    )
                )

    # Dimension 5 — named companies the user supplied.
    for named in companies[:8]:
        for title in titles[:2]:
            specs.append(
                QuerySpec(_compose(_quote(title), _quote(named)), "company", priority=1)
            )

    # Dimension 6 — prompt keywords not covered by a structured filter.
    for keyword in keywords[:6]:
        for title in titles[:3]:
            specs.append(
                QuerySpec(
                    _compose(_quote(title), keyword, _quote(primary_location)),
                    "keyword",
                    priority=3,
                )
            )

    # Dimension 7 — a US regional sweep when the user asked for the US, or when
    # they named no geography at all (global industry searches lean on the US index).
    if titles and (_wants_usa(location, prompt) or not location.strip()):
        for index, region in enumerate(_US_SEARCH_REGIONS[:10]):
            specs.append(
                QuerySpec(
                    _compose(
                        _quote(titles[index % min(3, len(titles))]), primary_sector, region
                    ),
                    "geography",
                    priority=4,
                )
            )

    if not specs and titles:
        specs.append(QuerySpec(_compose(_quote(titles[0])), "title"))

    return _dedupe(specs, max_queries)


def second_page(spec: QuerySpec) -> QuerySpec:
    """A follow-up page for a query that filled its first page."""
    return QuerySpec(spec.text, spec.dimension, page=spec.page + 1, priority=spec.priority + 5)
