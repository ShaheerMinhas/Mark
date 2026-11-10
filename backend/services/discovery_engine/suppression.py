"""Exclusions checked when writing the cache and when serving it."""

from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.models import GlobalPerson, SuppressionEntry
from backend.services.global_cache.normalize import normalize_domain, normalize_linkedin_url


def _values(*, email: str | None = None, domain: str | None = None, profile_url: str | None = None, provider_id: str | None = None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if email and "@" in email:
        found.append(("email", email.strip().lower()))
    normalized_domain = normalize_domain(domain)
    if normalized_domain:
        found.append(("domain", normalized_domain))
    profile = normalize_linkedin_url(profile_url or "")
    if profile:
        found.append(("profile", profile))
    if provider_id:
        found.append(("provider", provider_id.strip().lower()))
    return found


def is_suppressed(db: Session, **identity) -> bool:
    pairs = _values(**identity)
    if not pairs:
        return False
    clauses = [
        and_(SuppressionEntry.kind == kind, SuppressionEntry.normalized_value == value)
        for kind, value in pairs
    ]
    return db.query(SuppressionEntry.id).filter(or_(*clauses)).first() is not None


def suppressed_people_filter(db: Session, query):
    """Drop suppressed identities in SQL. The suppression list is the small side."""
    rows = db.query(SuppressionEntry.kind, SuppressionEntry.normalized_value).all()
    emails = [value for kind, value in rows if kind == "email"]
    domains = [value for kind, value in rows if kind == "domain"]
    profiles = [value for kind, value in rows if kind == "profile"]
    if emails:
        query = query.filter((GlobalPerson.email.is_(None)) | (~GlobalPerson.email.in_(emails)))
    if domains:
        query = query.filter((GlobalPerson.domain.is_(None)) | (~GlobalPerson.domain.in_(domains)))
    if profiles:
        query = query.filter(
            (GlobalPerson.profile_url_normalized.is_(None))
            | (~GlobalPerson.profile_url_normalized.in_(profiles))
        )
    return query


def suppress_and_archive(db: Session, person_id: int, *, user_id: int | None, reason: str) -> GlobalPerson | None:
    """Archive a shared person and suppress the identities we can name. Evidence stays. Workspace leads stay."""
    person = db.get(GlobalPerson, person_id)
    if person is None:
        return None
    from backend.services.discovery_engine.admin_read import archive_person

    archive_person(db, person_id)
    db.refresh(person)
    if person.email:
        add_suppression(db, kind="email", value=person.email, reason=reason, user_id=user_id)
    if person.profile_url:
        add_suppression(db, kind="profile", value=person.profile_url, reason=reason, user_id=user_id)
    if person.domain:
        add_suppression(db, kind="domain", value=person.domain, reason=reason, user_id=user_id)
    return person


def apply_retention(db: Session, *, now=None) -> int:
    """Archive shared people older than the operator retention window. This is not a GDPR deletion."""
    from datetime import datetime, timedelta

    from backend.services.discovery_engine.settings import ensure_settings

    settings = ensure_settings(db)
    days = int(settings.retention_days or 0)
    if days < 1:
        return 0
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=days)
    rows = (
        db.query(GlobalPerson)
        .filter(GlobalPerson.archived_at.is_(None), GlobalPerson.discovered_at < cutoff)
        .all()
    )
    count = 0
    for row in rows:
        suppress_and_archive(db, row.id, user_id=None, reason=f"retention {days} days")
        count += 1
    return count


def add_suppression(db: Session, *, kind: str, value: str, reason: str | None, user_id: int | None) -> SuppressionEntry:
    allowed = {"email", "domain", "profile", "provider"}
    if kind not in allowed:
        raise ValueError("Unknown suppression kind")
    normalized = value.strip().lower()
    if kind == "domain":
        normalized = normalize_domain(value) or ""
    elif kind == "profile":
        normalized = normalize_linkedin_url(value) or ""
    elif kind == "email":
        normalized = value.strip().lower()
    if not normalized:
        raise ValueError("Suppression value is empty after normalization")
    existing = (
        db.query(SuppressionEntry)
        .filter(SuppressionEntry.kind == kind, SuppressionEntry.normalized_value == normalized)
        .one_or_none()
    )
    if existing:
        return existing
    row = SuppressionEntry(kind=kind, normalized_value=normalized, reason=reason, created_by_user_id=user_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
