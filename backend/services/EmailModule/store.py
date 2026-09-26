"""In-memory and SQL stores for cache, patterns, evidence, and usage."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Protocol

from backend.services.EmailModule.types import EmailResult, utc_now


def person_key(name: str, company: str) -> str:
    return f"{(name or '').strip().lower()}|{(company or '').strip().lower()}"


class EmailStore(Protocol):
    def get_cached(self, name: str, company: str, domain: str = "") -> EmailResult | None: ...
    def save_cached(self, name: str, company: str, domain: str, result: EmailResult) -> None: ...
    def prior_person_email(self, name: str, company: str) -> EmailResult | None: ...
    def domain_examples(self, domain: str) -> list[tuple[str, str]]: ...
    def get_pattern(self, domain: str) -> dict[str, Any] | None: ...
    def observe_pattern(
        self, domain: str, pattern: str, email: str, confidence: float, sample_count: int
    ) -> None: ...
    def add_evidence(self, **kwargs: Any) -> None: ...
    def log_usage(self, **kwargs: Any) -> None: ...
    def save_verification(self, **kwargs: Any) -> None: ...


class MemoryEmailStore:
    def __init__(self, cache_days: int = 30) -> None:
        self.cache_days = cache_days
        self.cache: dict[str, tuple[datetime, EmailResult]] = {}
        self.leads: list[tuple[str, str, EmailResult]] = []
        self.patterns: dict[str, dict[str, Any]] = {}
        self.examples: dict[str, list[tuple[str, str]]] = {}
        self.evidence: list[dict[str, Any]] = []
        self.usage: list[dict[str, Any]] = []
        self.verifications: list[dict[str, Any]] = []

    def _fresh(self, saved: datetime) -> bool:
        if self.cache_days <= 0:
            return False
        return datetime.utcnow() - saved < timedelta(days=self.cache_days)

    def get_cached(self, name: str, company: str, domain: str = "") -> EmailResult | None:
        key = person_key(name, company) + "|" + (domain or "").lower()
        hit = self.cache.get(key)
        if not hit:
            return None
        saved, result = hit
        if not self._fresh(saved):
            return None
        copy = deepcopy(result)
        copy.cached = True
        return copy

    def save_cached(self, name: str, company: str, domain: str, result: EmailResult) -> None:
        key = person_key(name, company) + "|" + (domain or "").lower()
        self.cache[key] = (datetime.utcnow(), result)
        self.leads.append((name, company, result))
        if result.email and domain:
            self.examples.setdefault(domain.lower(), []).append((name, result.email))

    def prior_person_email(self, name: str, company: str) -> EmailResult | None:
        if self.cache_days <= 0:
            return None
        needle = person_key(name, company)
        for n, c, result in reversed(self.leads):
            if person_key(n, c) == needle and result.email and result.status != "not_found":
                return result
        return None

    def domain_examples(self, domain: str) -> list[tuple[str, str]]:
        return list(self.examples.get((domain or "").lower(), []))

    def get_pattern(self, domain: str) -> dict[str, Any] | None:
        return self.patterns.get((domain or "").lower())

    def observe_pattern(
        self, domain: str, pattern: str, email: str, confidence: float, sample_count: int
    ) -> None:
        key = (domain or "").lower()
        row = self.patterns.get(key) or {
            "domain": key,
            "pattern": pattern,
            "confidence": confidence,
            "sample_count": 0,
            "supporting_emails": [],
            "last_observed_at": utc_now(),
        }
        emails = list(row.get("supporting_emails") or [])
        if email.lower() not in emails:
            emails.append(email.lower())
        row.update(
            {
                "pattern": pattern,
                "confidence": confidence,
                "sample_count": max(int(row.get("sample_count") or 0), sample_count, len(emails)),
                "supporting_emails": emails,
                "last_observed_at": utc_now(),
            }
        )
        self.patterns[key] = row

    def add_evidence(self, **kwargs: Any) -> None:
        self.evidence.append(kwargs)

    def log_usage(self, **kwargs: Any) -> None:
        self.usage.append(kwargs)

    def save_verification(self, **kwargs: Any) -> None:
        self.verifications.append(kwargs)
