"""Structured target rules. Optional blanks do not drop a person. Nothing is deleted."""

from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.models import DiscoveryCategory, DiscoveryFieldRule, DiscoveryTarget, GlobalPerson, GlobalPersonTechnology
from backend.services.global_cache.normalize import country_code
from backend.services.identity import normalize_name, normalize_title

ALLOWED_COUNTRIES = {
    "US", "GB", "DE", "FR", "NL", "IE", "SE", "NO", "DK", "FI", "CH", "AT", "BE",
    "ES", "IT", "PT", "PL", "CZ", "EE", "LT", "LV", "RO",
}
US_REGION_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR", "GU",
}
FIELD_NAMES = (
    "country", "title", "industry", "technology", "employee_count", "function",
    "seniority", "department", "email", "phone",
)
INDUSTRY_PRESET = (
    "Software", "SaaS", "AI/ML", "Cloud", "Cybersecurity", "FinTech", "HealthTech",
    "EdTech", "Analytics", "IT services", "Dev tools", "Enterprise software",
    "Internet/ecommerce technology", "Robotics", "Telecom",
)
TECH_PRESET = (
    "AWS", "Azure", "GCP", "Salesforce", "HubSpot", "Shopify", "React", "Angular",
    "Kubernetes", "Docker", "Postgres", "Mongo",
)
FUNCTION_PRESET = (
    "engineering", "management", "it", "product", "data", "security", "devops",
    "sales", "marketing", "operations",
)
DEPARTMENT_PRESET = ("engineering", "product", "sales", "marketing", "operations", "it", "executive")
SUBINDUSTRY_PRESET = ("Developer tools", "Cloud infrastructure", "Application software")
SENIORITY_PRESET = ("c-level", "vp", "director", "manager", "individual")


def _seniority_key(value: str) -> str:
    folded = normalize_name(value)
    if folded in {"c level", "c-level"}:
        return "c-level"
    return folded


def _norm_list(values, transform) -> list[str]:
    found = []
    for value in values or []:
        text = transform(str(value))
        if text and text not in found:
            found.append(text)
    return found


def _country(value) -> str:
    raw = str(value).strip()
    if len(raw) == 2 and raw.isalpha():
        code = raw.upper()
    else:
        code = country_code(raw)
    if code not in ALLOWED_COUNTRIES:
        raise ValueError(f"Unsupported country {value}")
    return code


def _catalogue(db: Session, kind: str) -> set[str]:
    rows = (
        db.query(DiscoveryCategory.normalized)
        .filter(DiscoveryCategory.kind == kind, DiscoveryCategory.active.is_(True))
        .all()
    )
    return {row[0] for row in rows}


def validate_criteria(db: Session, raw: dict) -> dict:
    raw = raw or {}
    geo = raw.get("geography") or {}
    include_countries = [_country(value) for value in geo.get("include_countries") or []]
    exclude_countries = [_country(value) for value in geo.get("exclude_countries") or []]
    overlap = set(include_countries) & set(exclude_countries)
    if overlap:
        raise ValueError("A country cannot be both included and excluded")
    regions = [str(value).strip().upper() for value in geo.get("include_regions") or [] if str(value).strip()]
    for region in regions:
        if region not in US_REGION_CODES:
            raise ValueError(f"Unsupported state or territory {region}")
    cities = _norm_list(geo.get("include_cities"), normalize_name)
    exclude_cities = _norm_list(geo.get("exclude_cities"), normalize_name)
    if set(cities) & set(exclude_cities):
        raise ValueError("A city cannot be both included and excluded")

    industries = raw.get("industry") or {}
    include_industry = [str(value).strip() for value in industries.get("include") or [] if str(value).strip()]
    exclude_industry = [str(value).strip() for value in industries.get("exclude") or [] if str(value).strip()]
    known_industry = _catalogue(db, "industry")
    for name in include_industry + exclude_industry:
        if normalize_name(name) not in known_industry:
            raise ValueError(f"Unknown industry {name}")
    if {normalize_name(n) for n in include_industry} & {normalize_name(n) for n in exclude_industry}:
        raise ValueError("An industry cannot be both included and excluded")
    include_sub = _norm_list(industries.get("include_sub"), normalize_name)
    exclude_sub = _norm_list(industries.get("exclude_sub"), normalize_name)
    known_sub = _catalogue(db, "subindustry")
    for name in include_sub + exclude_sub:
        if name not in known_sub:
            raise ValueError(f"Unknown subindustry {name}")
    if set(include_sub) & set(exclude_sub):
        raise ValueError("A subindustry cannot be both included and excluded")

    titles = raw.get("titles") or {}
    exact = _norm_list(titles.get("exact"), normalize_title)
    synonyms = _norm_list(titles.get("synonyms"), normalize_title)
    excluded_titles = _norm_list(titles.get("exclude"), normalize_title)
    if set(exact + synonyms) & set(excluded_titles):
        raise ValueError("A title cannot be both requested and excluded")

    functions = _norm_list(raw.get("functions"), normalize_name)
    seniorities = _norm_list(raw.get("seniorities"), _seniority_key)
    departments = _norm_list(raw.get("departments"), normalize_name)
    known_functions = _catalogue(db, "function")
    known_seniority = _catalogue(db, "seniority")
    known_department = _catalogue(db, "department")
    for name in functions:
        if name not in known_functions:
            raise ValueError(f"Unknown function {name}")
    for name in seniorities:
        if name not in known_seniority:
            raise ValueError(f"Unknown seniority {name}")
    for name in departments:
        if name not in known_department:
            raise ValueError(f"Unknown department {name}")

    company = raw.get("company") or {}
    employee_min = company.get("employee_min")
    employee_max = company.get("employee_max")
    if employee_min is not None and int(employee_min) < 0:
        raise ValueError("employee_min cannot be negative")
    if employee_max is not None and int(employee_max) < 0:
        raise ValueError("employee_max cannot be negative")
    if employee_min is not None and employee_max is not None and int(employee_min) > int(employee_max):
        raise ValueError("employee_min cannot exceed employee_max")

    tech = raw.get("technologies") or {}
    mode = tech.get("mode") or "optional"
    if mode not in {"optional", "required"}:
        raise ValueError("technology mode must be optional or required")
    tech_names = _norm_list(tech.get("include"), normalize_name)
    known_tech = _catalogue(db, "technology")
    for name in tech_names:
        if name not in known_tech:
            raise ValueError(f"Unknown technology {name}")

    return {
        "geography": {
            "include_countries": include_countries,
            "exclude_countries": exclude_countries,
            "include_regions": regions,
            "include_cities": cities,
            "exclude_cities": exclude_cities,
        },
        "industry": {
            "include": [normalize_name(name) for name in include_industry],
            "exclude": [normalize_name(name) for name in exclude_industry],
            "include_sub": include_sub,
            "exclude_sub": exclude_sub,
        },
        "titles": {"exact": exact, "synonyms": synonyms, "exclude": excluded_titles},
        "functions": functions,
        "seniorities": seniorities,
        "departments": departments,
        "company": {
            "employee_min": None if employee_min is None else int(employee_min),
            "employee_max": None if employee_max is None else int(employee_max),
            "sizes": [str(value).strip() for value in company.get("sizes") or [] if str(value).strip()],
            "types": [str(value).strip() for value in company.get("types") or [] if str(value).strip()],
        },
        "technologies": {"mode": mode, "include": tech_names},
    }


def field_rules(db: Session) -> dict[str, DiscoveryFieldRule]:
    rows = db.query(DiscoveryFieldRule).all()
    return {row.field_name: row for row in rows}


def _required(rules: dict[str, DiscoveryFieldRule], name: str) -> bool:
    row = rules.get(name)
    return bool(row and row.collection_enabled and row.requirement == "required")


def _collected(rules: dict[str, DiscoveryFieldRule], name: str) -> bool:
    row = rules.get(name)
    return row is None or bool(row.collection_enabled)


def _text_clause(column, values: list[str], *, required: bool):
    folded = func.lower(column)
    clause = folded.in_([value.lower() for value in values])
    if required:
        return clause
    return or_(column.is_(None), column == "", clause)


class _RuleView:
    def __init__(self, row, requirement: str):
        self.collection_enabled = row.collection_enabled
        self.requirement = requirement


def preferred_as_required(rules: dict[str, DiscoveryFieldRule]) -> dict[str, _RuleView]:
    """Preferred fields count toward a stricter tier. They still do not delete rows."""
    return {
        name: _RuleView(row, "required" if row.requirement == "preferred" else row.requirement)
        for name, row in rules.items()
    }


def apply_criteria(db: Session, query, criteria: dict, rules: dict):
    """SQL filters. A missing optional value stays in the result. Rows are not deleted."""
    criteria = criteria or {}
    geo = criteria.get("geography") or {}
    if _collected(rules, "country"):
        include = geo.get("include_countries") or []
        exclude = geo.get("exclude_countries") or []
        if include and _required(rules, "country"):
            query = query.filter(GlobalPerson.country_code.in_(include))
        elif include:
            query = query.filter(or_(GlobalPerson.country_code.is_(None), GlobalPerson.country_code.in_(include)))
        elif _required(rules, "country"):
            query = query.filter(GlobalPerson.country_code.isnot(None))
        if exclude:
            query = query.filter(or_(GlobalPerson.country_code.is_(None), ~GlobalPerson.country_code.in_(exclude)))
        regions = [str(value).upper() for value in geo.get("include_regions") or []]
        if regions:
            query = query.filter(or_(GlobalPerson.region.is_(None), func.upper(GlobalPerson.region).in_(regions)))
        cities = [str(value).lower() for value in geo.get("include_cities") or []]
        if cities:
            query = query.filter(or_(GlobalPerson.city.is_(None), func.lower(GlobalPerson.city).in_(cities)))
        excluded_cities = [str(value).lower() for value in geo.get("exclude_cities") or []]
        if excluded_cities:
            query = query.filter(or_(GlobalPerson.city.is_(None), ~func.lower(GlobalPerson.city).in_(excluded_cities)))

    industry = criteria.get("industry") or {}
    if _collected(rules, "industry"):
        include = industry.get("include") or []
        if include:
            query = query.filter(_text_clause(GlobalPerson.industry, include, required=_required(rules, "industry")))
        elif _required(rules, "industry"):
            query = query.filter(GlobalPerson.industry.isnot(None), GlobalPerson.industry != "")
        if industry.get("exclude"):
            excluded = [value.lower() for value in industry["exclude"]]
            query = query.filter(
                or_(GlobalPerson.industry.is_(None), ~func.lower(GlobalPerson.industry).in_(excluded))
            )
        if industry.get("include_sub"):
            query = query.filter(
                _text_clause(GlobalPerson.subindustry, industry["include_sub"], required=_required(rules, "industry"))
            )
        if industry.get("exclude_sub"):
            excluded = [value.lower() for value in industry["exclude_sub"]]
            query = query.filter(
                or_(GlobalPerson.subindustry.is_(None), ~func.lower(GlobalPerson.subindustry).in_(excluded))
            )

    titles = criteria.get("titles") or {}
    if _collected(rules, "title"):
        wanted = [value.lower() for value in list(titles.get("exact") or []) + list(titles.get("synonyms") or [])]
        if wanted:
            clause = func.lower(GlobalPerson.title_normalized).in_(wanted)
            if not _required(rules, "title"):
                clause = or_(GlobalPerson.title_normalized.is_(None), GlobalPerson.title_normalized == "", clause)
            query = query.filter(clause)
        elif _required(rules, "title"):
            query = query.filter(GlobalPerson.title_normalized.isnot(None), GlobalPerson.title_normalized != "")
        if titles.get("exclude"):
            excluded = [value.lower() for value in titles["exclude"]]
            query = query.filter(
                or_(
                    GlobalPerson.title_normalized.is_(None),
                    ~func.lower(GlobalPerson.title_normalized).in_(excluded),
                )
            )

    for column, key, rule_name in (
        (GlobalPerson.job_function, "functions", "function"),
        (GlobalPerson.seniority, "seniorities", "seniority"),
        (GlobalPerson.department, "departments", "department"),
    ):
        if not _collected(rules, rule_name):
            continue
        values = criteria.get(key) or []
        if values:
            query = query.filter(_text_clause(column, values, required=_required(rules, rule_name)))
        elif _required(rules, rule_name):
            query = query.filter(column.isnot(None), column != "")

    company = criteria.get("company") or {}
    if _collected(rules, "employee_count"):
        low = company.get("employee_min")
        high = company.get("employee_max")
        if low is not None:
            clause = GlobalPerson.employee_count >= int(low)
            if not _required(rules, "employee_count"):
                clause = or_(GlobalPerson.employee_count.is_(None), clause)
            query = query.filter(clause)
        if high is not None:
            clause = GlobalPerson.employee_count <= int(high)
            if not _required(rules, "employee_count"):
                clause = or_(GlobalPerson.employee_count.is_(None), clause)
            query = query.filter(clause)
        if company.get("sizes"):
            query = query.filter(
                _text_clause(GlobalPerson.size_band, company["sizes"], required=_required(rules, "employee_count"))
            )
        if company.get("types"):
            query = query.filter(
                _text_clause(GlobalPerson.company_type, company["types"], required=_required(rules, "employee_count"))
            )

    tech = criteria.get("technologies") or {}
    names = [value.lower() for value in tech.get("include") or []]
    if _collected(rules, "technology"):
        has_any = GlobalPerson.id.in_(db.query(GlobalPersonTechnology.person_id))
        if names:
            has_listed = GlobalPerson.id.in_(
                db.query(GlobalPersonTechnology.person_id).filter(
                    func.lower(GlobalPersonTechnology.technology).in_(names)
                )
            )
            required = tech.get("mode") == "required" or _required(rules, "technology")
            query = query.filter(has_listed if required else or_(~has_any, has_listed))
        elif _required(rules, "technology") or tech.get("mode") == "required":
            query = query.filter(has_any)
    if _collected(rules, "email") and _required(rules, "email"):
        query = query.filter(GlobalPerson.email.isnot(None), GlobalPerson.email != "")
    if _collected(rules, "phone") and _required(rules, "phone"):
        query = query.filter(GlobalPerson.phone.isnot(None), GlobalPerson.phone != "")
    return query


def seed_defaults(db: Session) -> None:
    """Catalogue and one paused preset. Does not create people or start a run."""
    def add_category(kind: str, name: str) -> None:
        normalized = _seniority_key(name) if kind == "seniority" else normalize_name(name)
        exists = (
            db.query(DiscoveryCategory)
            .filter(DiscoveryCategory.kind == kind, DiscoveryCategory.normalized == normalized)
            .first()
        )
        if exists is None:
            db.add(DiscoveryCategory(kind=kind, name=name, normalized=normalized, active=True))

    for name in INDUSTRY_PRESET:
        add_category("industry", name)
    for name in SUBINDUSTRY_PRESET:
        add_category("subindustry", name)
    for name in TECH_PRESET:
        add_category("technology", name)
    for name in FUNCTION_PRESET:
        add_category("function", name)
    for name in SENIORITY_PRESET:
        add_category("seniority", name)
    for name in DEPARTMENT_PRESET:
        add_category("department", name)
    defaults = {
        "country": ("required", 10),
        "title": ("required", 20),
        "industry": ("preferred", 30),
        "technology": ("optional", 80),
        "employee_count": ("optional", 70),
        "function": ("preferred", 40),
        "seniority": ("preferred", 50),
        "department": ("optional", 60),
        "email": ("preferred", 15),
        "phone": ("optional", 90),
    }
    for name, (requirement, priority) in defaults.items():
        row = db.query(DiscoveryFieldRule).filter(DiscoveryFieldRule.field_name == name).one_or_none()
        if row is None:
            db.add(
                DiscoveryFieldRule(
                    field_name=name,
                    collection_enabled=True,
                    requirement=requirement,
                    priority=priority,
                    refresh_hours=168,
                    enrichment_source=None,
                    verification_required=False,
                )
            )
    preset_name = "US and Europe technology leaders"
    if db.query(DiscoveryTarget).filter(DiscoveryTarget.name == preset_name).first() is None:
        db.add(
            DiscoveryTarget(
                name=preset_name,
                scheduling_status="paused",
                schedule_type="weekdays",
                timezone="UTC",
                hour=7,
                minute=0,
                countries=sorted(ALLOWED_COUNTRIES),
                industries=list(INDUSTRY_PRESET),
                titles=["cto", "cio", "ceo"],
                criteria={
                    "geography": {
                        "include_countries": sorted(ALLOWED_COUNTRIES),
                        "exclude_countries": [],
                        "include_regions": [],
                        "include_cities": [],
                        "exclude_cities": [],
                    },
                    "industry": {
                        "include": [normalize_name(name) for name in INDUSTRY_PRESET],
                        "exclude": [],
                        "include_sub": [],
                        "exclude_sub": [],
                    },
                    "titles": {
                        "exact": ["cto", "cio", "ceo"],
                        "synonyms": ["chief technology officer", "founder"],
                        "exclude": ["intern"],
                    },
                    "functions": ["engineering", "management", "it"],
                    "seniorities": ["c-level"],
                    "departments": [],
                    "company": {"employee_min": None, "employee_max": None, "sizes": [], "types": []},
                    "technologies": {
                        "mode": "optional",
                        "include": [normalize_name(name) for name in TECH_PRESET],
                    },
                },
                next_run_at=None,
            )
        )
    db.flush()
