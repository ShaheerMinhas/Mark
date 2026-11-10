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
