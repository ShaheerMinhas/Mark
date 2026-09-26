from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base


class AccountType(str, enum.Enum):
    INDIVIDUAL = "individual"
    ORGANIZATION = "organization"


class OrgRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class SearchJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[AccountType] = mapped_column(Enum(AccountType), default=AccountType.INDIVIDUAL)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    memberships: Mapped[list[OrganizationMember]] = relationship(back_populates="user")
    linkedin_sessions: Mapped[list[LinkedInSession]] = relationship(back_populates="user")
    searches: Mapped[list[SearchJob]] = relationship(back_populates="owner")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    members: Mapped[list[OrganizationMember]] = relationship(back_populates="organization")
    searches: Mapped[list[SearchJob]] = relationship(back_populates="organization")


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role: Mapped[OrgRole] = mapped_column(Enum(OrgRole), default=OrgRole.MEMBER)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    organization: Mapped[Organization] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class LinkedInSession(Base):
    __tablename__ = "linkedin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(255), default="Primary")
    encrypted_cookie: Mapped[str] = mapped_column(Text)
    linkedin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship(back_populates="linkedin_sessions")


class SearchJob(Base):
    __tablename__ = "search_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(50), default="public")
    status: Mapped[SearchJobStatus] = mapped_column(
        Enum(
            SearchJobStatus,
            values_callable=_enum_values,
            native_enum=False,
            length=50,
        ),
        default=SearchJobStatus.PENDING,
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    progress: Mapped[int] = mapped_column(Integer, default=0)

    owner: Mapped[User] = relationship(back_populates="searches")
    organization: Mapped[Organization | None] = relationship(back_populates="searches")
    leads: Mapped[list[LeadRecord]] = relationship(back_populates="search_job", cascade="all, delete-orphan")


class LeadRecord(Base):
    __tablename__ = "lead_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_job_id: Mapped[int] = mapped_column(ForeignKey("search_jobs.id"), index=True)

    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    personal_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ai_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    work_designation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_score: Mapped[float] = mapped_column(default=0.0)
    activity_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    company_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    email_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_pattern: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_evidence: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    email_verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email_enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    search_job: Mapped[SearchJob] = relationship(back_populates="leads")


class DomainEmailPattern(Base):
    __tablename__ = "domain_email_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    pattern: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    supporting_emails: Mapped[list] = mapped_column(JSON, default=list)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailEvidence(Base):
    __tablename__ = "email_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("lead_records.id"), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailVerificationEvent(Base):
    __tablename__ = "email_verification_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("lead_records.id"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    credits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lead_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailLookupCache(Base):
    __tablename__ = "email_lookup_cache"
    __table_args__ = (UniqueConstraint("person_key", "domain", name="uq_email_cache_person_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_key: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(255), default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailEnrichmentJob(Base):
    __tablename__ = "email_enrichment_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_job_id: Mapped[int] = mapped_column(ForeignKey("search_jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    found: Mapped[int] = mapped_column(Integer, default=0)
    inferred: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[int] = mapped_column(Integer, default=0)
    not_found: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
