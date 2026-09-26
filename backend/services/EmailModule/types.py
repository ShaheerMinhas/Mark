"""Shared email-intelligence types. EmailHit is kept for older callers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EmailStatus(str, Enum):
    FOUND = "found"
    INFERRED = "inferred"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    VERIFY_UNCERTAIN = "verify_uncertain"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class VerifyStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"
    ACCEPT_ALL = "accept_all"
    DISPOSABLE = "disposable"
    ROLE_BASED = "role_based"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class EmailHit:
    email: str
    source: str
    confidence: str
    evidence: str = ""
    domain: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class PersonInput:
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    job_title: str = ""
    company_name: str = ""
    company_domain: str = ""
    company_website: str = ""
    linkedin_url: str = ""
    snippet: str = ""
    lead_id: int | None = None
    search_id: int | None = None


@dataclass
class Evidence:
    type: str
    observed_at: str = ""
    url: str = ""
    provider: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class Verification:
    status: str = VerifyStatus.UNKNOWN.value
    checked_at: str = ""
    provider: str = ""
    mx: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class EmailResult:
    email: str | None = None
    status: str = EmailStatus.NOT_FOUND.value
    confidence: str | None = None
    source: str = ""
    source_url: str = ""
    pattern: str = ""
    pattern_confidence: float | None = None
    evidence: list[Evidence] = field(default_factory=list)
    verification: Verification | None = None
    domain: str = ""
    discovered_at: str = ""
    last_verified_at: str = ""
    cached: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "status": self.status,
            "confidence": self.confidence,
            "source": self.source,
            "source_url": self.source_url,
            "pattern": self.pattern,
            "pattern_confidence": self.pattern_confidence,
            "evidence": [item.to_dict() for item in self.evidence],
            "verification": self.verification.to_dict() if self.verification else None,
            "domain": self.domain,
            "discovered_at": self.discovered_at,
            "last_verified_at": self.last_verified_at,
            "cached": self.cached,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EmailResult:
        data = data or {}
        evidence = [
            Evidence(**{k: item.get(k) or "" for k in ("type", "observed_at", "url", "provider", "detail")})
            for item in (data.get("evidence") or [])
            if isinstance(item, dict) and item.get("type")
        ]
        verification = None
        raw_v = data.get("verification")
        if isinstance(raw_v, dict) and raw_v:
            verification = Verification(**{k: raw_v.get(k) or "" for k in ("status", "checked_at", "provider", "mx", "detail")})
        return cls(
            email=data.get("email"),
            status=data.get("status") or EmailStatus.NOT_FOUND.value,
            confidence=data.get("confidence"),
            source=data.get("source") or "",
            source_url=data.get("source_url") or "",
            pattern=data.get("pattern") or "",
            pattern_confidence=data.get("pattern_confidence"),
            evidence=evidence,
            verification=verification,
            domain=data.get("domain") or "",
            discovered_at=data.get("discovered_at") or "",
            last_verified_at=data.get("last_verified_at") or "",
            cached=bool(data.get("cached")),
            notes=data.get("notes") or "",
        )


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def not_found(notes: str = "") -> EmailResult:
    return EmailResult(status=EmailStatus.NOT_FOUND.value, notes=notes, discovered_at=utc_now())
