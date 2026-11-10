"""Idempotent upserts into the shared cache.

A name on its own is never an identity. Conflicting stronger keys are not merged.
Private workspace fields are refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import GlobalCompany, GlobalEvidence, GlobalIdentityKey, GlobalPerson, GlobalPersonTechnology
from backend.services.global_cache.normalize import (
    clean_email_status,
    country_code,
    function_from_title,
    normalize_domain,
    normalize_technology,
    seniority_from_title,
    split_name,
)
from backend.services.global_cache.policy import load_rules, quality_tier
from backend.services.identity import normalize_company, normalize_linkedin_url, normalize_name, normalize_title


class PrivateEvidenceError(Exception):
    """Workspace-owned enrichment must not enter the shared cache."""


@dataclass
class PublicProspect:
    full_name: str
    given_name: str | None = None
    family_name: str | None = None
    title: str | None = None
    job_function: str | None = None
    department: str | None = None
    seniority: str | None = None
    company_name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    subindustry: str | None = None
    employee_count: int | None = None
    company_size: str | None = None
    company_type: str | None = None
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    profile_url: str | None = None
    email: str | None = None
    email_status: str | None = None
    email_confidence: str | None = None
    phone: str | None = None
    phone_evidenced: bool = False
    technologies: list[str] = field(default_factory=list)
    source_url: str | None = None
    provider: str | None = None
    provider_record_id: str | None = None
    share_scope: str = "public"


def refuse_workspace_lead(lead) -> None:
    """Existing customer leads are private. This never copies them."""
    raise PrivateEvidenceError(
        f"Lead {getattr(lead, 'id', None)} stays in its workspace and was not added to the shared cache"
    )


def _blank(value) -> bool:
    return value is None or value == "" or value == {}


def _fill(current, new):
    if _blank(new):
        return current
    if _blank(current):
        return new
    return current


def _keys(prospect: PublicProspect, domain: str | None, profile: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if profile:
        found.append(("profile", profile))
    email = (prospect.email or "").strip().lower()
    if email and "@" in email:
        found.append(("email", email))
    provider = (prospect.provider or "").strip().lower()
    provider_id = (prospect.provider_record_id or "").strip()
    if provider and provider_id:
        found.append(("provider", f"{provider}:{provider_id}"))
    person = normalize_name(prospect.full_name)
    if person and domain:
        found.append(("name_domain", f"{person}|{domain}"))
    return found


def _lookup(db: Session, keys: list[tuple[str, str]]) -> dict[str, int]:
    if not keys:
        return {}
    rows = (
        db.query(GlobalIdentityKey)
        .filter(GlobalIdentityKey.subject_type == "person")
        .filter(GlobalIdentityKey.kind.in_([k for k, _ in keys]))
        .all()
    )
    wanted = {f"{kind}:{value}" for kind, value in keys}
    out: dict[str, int] = {}
    for row in rows:
        token = f"{row.kind}:{row.key_value}"
        if token in wanted:
            out[row.kind] = row.subject_id
    return out


def _choose_person_id(hits: dict[str, int]) -> tuple[int | None, bool]:
    """Prefer profile, then email, then provider, then name+domain. Conflicts do not merge."""
    order = ("profile", "email", "provider", "name_domain")
    chosen = None
    conflict = False
    for kind in order:
        if kind not in hits:
            continue
        if chosen is None:
            chosen = hits[kind]
        elif hits[kind] != chosen:
            conflict = True
    return chosen, conflict


def _upsert_company(db: Session, prospect: PublicProspect, domain: str | None, code: str | None) -> GlobalCompany | None:
    name = (prospect.company_name or "").strip() or None
    normalized = normalize_company(name or "") or None
    if not domain and not normalized:
        return None
    row = None
    if domain:
        row = db.query(GlobalCompany).filter(GlobalCompany.domain == domain).one_or_none()
    if row is None and normalized and not domain:
        row = (
            db.query(GlobalCompany)
            .filter(GlobalCompany.name_normalized == normalized, GlobalCompany.domain.is_(None))
            .one_or_none()
        )
    if row is None:
        row = GlobalCompany(
            name=name,
            name_normalized=normalized,
            domain=domain,
            website=(prospect.website or "").strip() or None,
            industry=(prospect.industry or "").strip() or None,
            subindustry=(prospect.subindustry or "").strip() or None,
            employee_count=prospect.employee_count,
            size_band=(prospect.company_size or "").strip() or None,
            company_type=(prospect.company_type or "").strip() or None,
            country_code=code,
            discovered_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        db.flush()
        return row
    if normalized and row.name_normalized and normalized != row.name_normalized and domain:
        # Same domain, different public name: keep the stored name and do not invent a blend.
        pass
    else:
        row.name = _fill(row.name, name)
        row.name_normalized = _fill(row.name_normalized, normalized)
    row.website = _fill(row.website, (prospect.website or "").strip() or None)
    row.industry = _fill(row.industry, (prospect.industry or "").strip() or None)
    row.subindustry = _fill(row.subindustry, (prospect.subindustry or "").strip() or None)
    row.employee_count = _fill(row.employee_count, prospect.employee_count)
    row.size_band = _fill(row.size_band, (prospect.company_size or "").strip() or None)
    row.company_type = _fill(row.company_type, (prospect.company_type or "").strip() or None)
    row.country_code = _fill(row.country_code, code)
    if domain and not row.domain:
        row.domain = domain
    row.updated_at = datetime.utcnow()
    return row


def _add_evidence(db: Session, *, subject_type: str, subject_id: int, field_name: str, value, prospect: PublicProspect, eligible: bool) -> None:
    if _blank(value):
        return
    text = str(value)[:2000]
    existing = (
        db.query(GlobalEvidence)
        .filter(
            GlobalEvidence.subject_type == subject_type,
            GlobalEvidence.subject_id == subject_id,
            GlobalEvidence.field_name == field_name,
            GlobalEvidence.value_text == text,
            GlobalEvidence.source_url == prospect.source_url,
        )
        .first()
    )
    if existing:
        return
    db.add(
        GlobalEvidence(
            subject_type=subject_type,
            subject_id=subject_id,
            field_name=field_name,
            value_text=text,
            source_url=prospect.source_url,
            provider=prospect.provider,
            provider_record_id=prospect.provider_record_id,
            share_scope="public",
            eligible=eligible,
            observed_at=datetime.utcnow(),
        )
    )


def _bind_keys(db: Session, person_id: int, keys: list[tuple[str, str]], blocked: set[str]) -> None:
    for kind, value in keys:
        if kind in blocked:
            continue
        existing = (
            db.query(GlobalIdentityKey)
            .filter(GlobalIdentityKey.kind == kind, GlobalIdentityKey.key_value == value)
            .one_or_none()
        )
        if existing:
            continue
        db.add(
            GlobalIdentityKey(
                kind=kind,
                key_value=value,
                subject_type="person",
                subject_id=person_id,
            )
        )


def upsert_public_prospect(db: Session, prospect: PublicProspect, _attempt: int = 0) -> GlobalPerson:
    if (prospect.share_scope or "public") != "public":
        raise PrivateEvidenceError("Only public evidence can enter the shared cache")
    if not (prospect.full_name or "").strip() and not prospect.profile_url and not prospect.email:
        raise ValueError("A public prospect needs a name, profile URL, or email")

    domain = normalize_domain(prospect.domain or prospect.website or prospect.email)
    profile = normalize_linkedin_url(prospect.profile_url or "")
    code = country_code(prospect.country_code) or country_code(prospect.region) or country_code(prospect.city)
    keys = _keys(prospect, domain, profile)
    hits = _lookup(db, keys)
    person_id, conflict = _choose_person_id(hits)
    incoming_name = normalize_name(prospect.full_name)
    hold_email = False
    if person_id is not None:
        existing = db.get(GlobalPerson, person_id)
        names_conflict = bool(
            existing and existing.name_normalized and incoming_name and existing.name_normalized != incoming_name
        )
        profiles_conflict = bool(
            existing
            and existing.profile_url_normalized
            and profile
            and existing.profile_url_normalized != profile
        )
        if names_conflict or profiles_conflict:
            person_id = None
            conflict = True
            hold_email = True
    rules = load_rules(db)
    company = _upsert_company(db, prospect, domain, code)

    given = (prospect.given_name or "").strip() or None
    family = (prospect.family_name or "").strip() or None
    if prospect.full_name and (not given or not family):
        split_given, split_family = split_name(prospect.full_name)
        given = given or split_given
        family = family or split_family
    title = (prospect.title or "").strip() or None
    seniority = (prospect.seniority or "").strip().lower() or seniority_from_title(title)
    function = (prospect.job_function or "").strip().lower() or function_from_title(title)
    email = (prospect.email or "").strip().lower() or None
    status = clean_email_status(prospect.email_status)
    phone = (prospect.phone or "").strip() if prospect.phone_evidenced else None

    if person_id is None:
        person = GlobalPerson(
            full_name=(prospect.full_name or "").strip() or None,
            given_name=given,
            family_name=family,
            name_normalized=normalize_name(prospect.full_name) or None,
            discovered_at=datetime.utcnow(),
        )
        db.add(person)
        db.flush()
    else:
        person = db.get(GlobalPerson, person_id)

    person.given_name = _fill(person.given_name, given)
    person.family_name = _fill(person.family_name, family)
    person.full_name = _fill(person.full_name, (prospect.full_name or "").strip() or None)
    person.name_normalized = _fill(person.name_normalized, normalize_name(prospect.full_name) or None)
    person.title = _fill(person.title, title)
    person.title_normalized = _fill(person.title_normalized, normalize_title(title or "") or None)
    person.job_function = _fill(person.job_function, function)
    person.department = _fill(person.department, (prospect.department or "").strip() or None)
    person.seniority = _fill(person.seniority, seniority)
    if company is not None and person.company_id is None:
        person.company_id = company.id
    person.company_name = _fill(person.company_name, (prospect.company_name or "").strip() or None)
    person.company_name_normalized = _fill(person.company_name_normalized, normalize_company(prospect.company_name or "") or None)
    person.domain = _fill(person.domain, domain)
    person.industry = _fill(person.industry, (prospect.industry or "").strip() or None)
    person.subindustry = _fill(person.subindustry, (prospect.subindustry or "").strip() or None)
    person.employee_count = _fill(person.employee_count, prospect.employee_count)
    person.size_band = _fill(person.size_band, (prospect.company_size or "").strip() or None)
    person.company_type = _fill(person.company_type, (prospect.company_type or "").strip() or None)
    person.country_code = _fill(person.country_code, code)
    person.region = _fill(person.region, (prospect.region or "").strip() or None)
    person.city = _fill(person.city, (prospect.city or "").strip() or None)
    if profile and not person.profile_url_normalized:
        person.profile_url = prospect.profile_url
        person.profile_url_normalized = profile

    blocked: set[str] = set()
    if hold_email:
        blocked.add("email")
        _add_evidence(
            db,
            subject_type="person",
            subject_id=person.id,
            field_name="email_conflict",
            value=email,
            prospect=prospect,
            eligible=False,
        )
    elif conflict and email and hits.get("email") not in {None, person.id}:
        blocked.add("email")
        _add_evidence(
            db,
            subject_type="person",
            subject_id=person.id,
            field_name="email_conflict",
            value=email,
            prospect=prospect,
            eligible=False,
        )
    elif email:
        if person.email_status == "verified" and status != "verified":
            blocked.add("email")
        elif person.email and email != person.email and person.email_status == "verified":
            blocked.add("email")
        else:
            if _blank(person.email) or (status == "verified" and person.email_status != "verified"):
                person.email = email
                person.email_status = status
                person.email_confidence = (prospect.email_confidence or "").strip() or None
                if status == "verified":
                    person.verified_at = datetime.utcnow()
            elif _blank(person.email_status):
                person.email_status = status
    if phone and _blank(person.phone):
        person.phone = phone

    has_identity = bool(person.profile_url_normalized or (person.name_normalized and person.domain))
    has_role = bool(person.title or person.job_function or person.company_name)
    tier, score = quality_tier(
        rules=rules,
        email=person.email,
        email_status=person.email_status,
        has_identity=has_identity,
        has_role=has_role,
    )
    person.quality_tier = tier
    person.quality_score = score
    person.updated_at = datetime.utcnow()
    if person.title and person.company_name and person.enriched_at is None and tier in {"enriched", "contactable", "verified"}:
        person.enriched_at = datetime.utcnow()

    for tech in prospect.technologies or []:
        normalized = normalize_technology(tech)
        if not normalized:
            continue
        exists = (
            db.query(GlobalPersonTechnology)
            .filter(
                GlobalPersonTechnology.person_id == person.id,
                GlobalPersonTechnology.technology == normalized,
            )
            .first()
        )
        if exists is None:
            db.add(GlobalPersonTechnology(person_id=person.id, technology=normalized))

    for field_name, value in (
        ("full_name", person.full_name),
        ("title", person.title),
        ("email", None if "email" in blocked else person.email),
        ("email_status", person.email_status),
        ("domain", person.domain),
        ("profile_url", person.profile_url_normalized),
        ("phone", person.phone),
        ("country_code", person.country_code),
    ):
        _add_evidence(db, subject_type="person", subject_id=person.id, field_name=field_name, value=value, prospect=prospect, eligible=True)

    try:
        _bind_keys(db, person.id, keys, blocked)
        db.commit()
    except IntegrityError:
        db.rollback()
        if _attempt >= 1:
            raise
        return upsert_public_prospect(db, prospect, _attempt=1)
    db.refresh(person)
    return person
