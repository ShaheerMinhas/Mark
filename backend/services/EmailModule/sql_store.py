from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from backend.models import (
    DomainEmailPattern,
    EmailEvidence,
    EmailLookupCache,
    EmailVerificationEvent,
    LeadRecord,
    ProviderUsage,
)
from backend.services.EmailModule.store import person_key
from backend.services.EmailModule.types import EmailResult, utc_now


class SqlEmailStore:
    def __init__(self, db: Session, cache_days: int = 30) -> None:
        self.db = db
        self.cache_days = cache_days

    def get_cached(self, name: str, company: str, domain: str = "") -> EmailResult | None:
        row = (
            self.db.query(EmailLookupCache)
            .filter(
                EmailLookupCache.person_key == person_key(name, company),
                EmailLookupCache.domain == (domain or ""),
            )
            .first()
        )
        if not row:
            return None
        if datetime.utcnow() - (row.created_at or datetime.utcnow()) > timedelta(days=self.cache_days):
            return None
        result = EmailResult.from_dict(row.result or {})
        result.cached = True
        return result

    def save_cached(self, name: str, company: str, domain: str, result: EmailResult) -> None:
        key = person_key(name, company)
        host = domain or ""
        row = (
            self.db.query(EmailLookupCache)
            .filter(EmailLookupCache.person_key == key, EmailLookupCache.domain == host)
            .first()
        )
        payload = result.to_dict()
        if row:
            row.result = payload
            row.created_at = datetime.utcnow()
            flag_modified(row, "result")
        else:
            self.db.add(
                EmailLookupCache(person_key=key, domain=host, result=payload, created_at=datetime.utcnow())
            )
        self.db.commit()

    def prior_person_email(self, name: str, company: str) -> EmailResult | None:
        row = (
            self.db.query(LeadRecord)
            .filter(LeadRecord.name == name, LeadRecord.company_name == company)
            .filter(LeadRecord.email.isnot(None))
            .order_by(LeadRecord.id.desc())
            .first()
        )
        if not row or not row.email:
            return None
        if datetime.utcnow() - (row.email_enriched_at or row.created_at or datetime.utcnow()) > timedelta(
            days=self.cache_days
        ):
            return None
        return EmailResult(
            email=row.email,
            status=row.email_status or "found",
            confidence=row.email_confidence,
            source=row.email_source or "",
            source_url=row.email_source_url or "",
            pattern=row.email_pattern or "",
            domain=row.company_domain or "",
            last_verified_at=row.email_verified_at.isoformat() + "Z" if row.email_verified_at else "",
            cached=True,
        )

    def domain_examples(self, domain: str) -> list[tuple[str, str]]:
        host = (domain or "").lower()
        if not host:
            return []
        rows = (
            self.db.query(LeadRecord)
            .filter(LeadRecord.company_domain == host, LeadRecord.email.isnot(None))
            .all()
        )
        pattern = self.get_pattern(host)
        extras = []
        if pattern:
            extras = [( "", email) for email in (pattern.get("supporting_emails") or [])]
        return [(r.name or "", r.email or "") for r in rows if r.email] + extras

    def get_pattern(self, domain: str) -> dict[str, Any] | None:
        row = self.db.query(DomainEmailPattern).filter(DomainEmailPattern.domain == (domain or "").lower()).first()
        if not row:
            return None
        return {
            "domain": row.domain,
            "pattern": row.pattern,
            "confidence": row.confidence,
            "sample_count": row.sample_count,
            "supporting_emails": row.supporting_emails or [],
            "last_observed_at": row.last_observed_at.isoformat() if row.last_observed_at else "",
        }

    def observe_pattern(
        self, domain: str, pattern: str, email: str, confidence: float, sample_count: int
    ) -> None:
        host = (domain or "").lower()
        row = self.db.query(DomainEmailPattern).filter(DomainEmailPattern.domain == host).first()
        emails = list((row.supporting_emails if row else []) or [])
        if email and email.lower() not in emails:
            emails.append(email.lower())
        if row:
            row.pattern = pattern
            row.confidence = confidence
            row.sample_count = max(row.sample_count or 0, sample_count, len(emails))
            row.supporting_emails = emails
            row.last_observed_at = datetime.utcnow()
            flag_modified(row, "supporting_emails")
        else:
            self.db.add(
                DomainEmailPattern(
                    domain=host,
                    pattern=pattern,
                    confidence=confidence,
                    sample_count=max(sample_count, len(emails)),
                    supporting_emails=emails,
                    last_observed_at=datetime.utcnow(),
                )
            )
        self.db.commit()

    def add_evidence(self, **kwargs: Any) -> None:
        self.db.add(
            EmailEvidence(
                lead_id=kwargs.get("lead_id"),
                email=kwargs.get("email"),
                type=kwargs.get("type") or "UNKNOWN",
                url=kwargs.get("url"),
                provider=kwargs.get("provider"),
                detail=kwargs.get("detail"),
                observed_at=datetime.utcnow(),
            )
        )
        self.db.commit()

    def log_usage(self, **kwargs: Any) -> None:
        self.db.add(
            ProviderUsage(
                provider=str(kwargs.get("provider") or ""),
                operation=str(kwargs.get("operation") or "find"),
                success=bool(kwargs.get("success")),
                credits=kwargs.get("credits"),
                lead_id=kwargs.get("lead_id"),
                search_id=kwargs.get("search_id"),
                http_status=kwargs.get("http_status"),
                detail=str(kwargs.get("detail") or "")[:500] or None,
                created_at=datetime.utcnow(),
            )
        )
        self.db.commit()

    def save_verification(self, **kwargs: Any) -> None:
        self.db.add(
            EmailVerificationEvent(
                lead_id=kwargs.get("lead_id"),
                email=str(kwargs.get("email") or ""),
                provider=str(kwargs.get("provider") or "local"),
                status=str(kwargs.get("status") or "unknown"),
                detail=kwargs.get("detail"),
                created_at=datetime.utcnow(),
            )
        )
        self.db.commit()
