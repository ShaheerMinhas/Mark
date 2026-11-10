from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
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
    MANAGER = "manager"
    MEMBER = "member"
    VIEWER = "viewer"


LEAD_LIFECYCLE_STAGES = (
    "new",
    "unreviewed",
    "qualified",
    "contacted",
    "interested",
    "not_interested",
    "converted",
    "disqualified",
    "archived",
    # legacy stages preserved so existing rows & callers keep working
    "meeting",
    "won",
    "lost",
)


class SearchJobStatus(str, enum.Enum):
    PENDING = "pending"
    # RUNNING predates the staged pipeline. Legacy rows still carry it, and the
    # pre-pipeline API surface collapses the granular stages back down to it.
    RUNNING = "running"
    DISCOVERING = "discovering"
    FILTERING = "filtering"
    ENRICHING = "enriching"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


OPEN_SEARCH_STATUSES = frozenset(
    {
        SearchJobStatus.PENDING,
        SearchJobStatus.RUNNING,
        SearchJobStatus.DISCOVERING,
        SearchJobStatus.FILTERING,
        SearchJobStatus.ENRICHING,
    }
)

TERMINAL_SEARCH_STATUSES = frozenset(
    {
        SearchJobStatus.COMPLETED,
        SearchJobStatus.PARTIALLY_COMPLETED,
        SearchJobStatus.FAILED,
        SearchJobStatus.CANCELLED,
    }
)

#: Statuses the pre-pipeline API contract knows about.
LEGACY_SEARCH_STATUSES = {
    SearchJobStatus.DISCOVERING: SearchJobStatus.RUNNING,
    SearchJobStatus.FILTERING: SearchJobStatus.RUNNING,
    SearchJobStatus.ENRICHING: SearchJobStatus.RUNNING,
    SearchJobStatus.PARTIALLY_COMPLETED: SearchJobStatus.COMPLETED,
    SearchJobStatus.CANCELLED: SearchJobStatus.FAILED,
}


class CandidateStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    QUALIFIED = "qualified"
    REJECTED = "rejected"


class EnrichmentStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    ENRICHED = "enriched"
    NO_MATCH = "no_match"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


class QueryRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    FAILED = "failed"


class JobType(str, enum.Enum):
    SEARCH_DISCOVERY = "search_discovery"
    SEARCH_ENRICHMENT = "search_enrichment"
    DISCOVERY_PIPELINE = "discovery_pipeline"


class QueuedJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
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
    #: Platform operator. Never granted by registration or by owning a workspace.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
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
    __table_args__ = (
        Index("ix_search_jobs_owner_created", "owner_id", "created_at"),
        Index("ix_search_jobs_org_created", "organization_id", "created_at"),
    )

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

    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: User-safe summary. Internal exception text stays in the logs.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    total_queries: Mapped[int] = mapped_column(Integer, default=0)
    completed_queries: Mapped[int] = mapped_column(Integer, default=0)
    failed_queries: Mapped[int] = mapped_column(Integer, default=0)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    candidates_deduplicated: Mapped[int] = mapped_column(Integer, default=0)
    qualified_leads: Mapped[int] = mapped_column(Integer, default=0)
    enrichment_attempted: Mapped[int] = mapped_column(Integer, default=0)
    enrichment_completed: Mapped[int] = mapped_column(Integer, default=0)
    emails_found: Mapped[int] = mapped_column(Integer, default=0)
    phones_found: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    #: People copied from the shared public cache into this search. Not a second lead model.
    cache_served: Mapped[int] = mapped_column(Integer, default=0)

    owner: Mapped[User] = relationship(back_populates="searches")
    organization: Mapped[Organization | None] = relationship(back_populates="searches")
    leads: Mapped[list[LeadRecord]] = relationship(back_populates="search_job", cascade="all, delete-orphan")
    candidates: Mapped[list[SearchCandidate]] = relationship(
        back_populates="search_job", cascade="all, delete-orphan"
    )
    query_runs: Mapped[list[SearchQueryRun]] = relationship(
        back_populates="search_job", cascade="all, delete-orphan"
    )


class LeadRecord(Base):
    __tablename__ = "lead_records"
    __table_args__ = (
        # A unique index rather than a UniqueConstraint: SQLite cannot add table
        # constraints after the fact, but CREATE UNIQUE INDEX works on both backends.
        Index("uq_lead_search_identity", "search_job_id", "identity_key", unique=True),
        Index("ix_lead_records_job_score", "search_job_id", "relevance_score"),
        Index("ix_lead_records_job_enrichment", "search_job_id", "enrichment_status"),
        Index("ix_lead_records_company", "company_name"),
        Index("ix_lead_records_email", "email"),
        Index("ix_lead_records_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_job_id: Mapped[int] = mapped_column(ForeignKey("search_jobs.id"), index=True)

    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
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

    identity_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    linkedin_url_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_candidates.id"), nullable=True
    )
    enrichment_status: Mapped[str] = mapped_column(
        String(32), default=EnrichmentStatus.PENDING.value
    )
    enrichment_attempts: Mapped[int] = mapped_column(Integer, default=0)
    enrichment_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enrichment_locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: CRM pipeline. Independent of discovery status.
    crm_stage: Mapped[str] = mapped_column(String(32), default="new")
    crm_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Mark 2.0 lead management & evidence-based sub-scores.
    lead_status: Mapped[str] = mapped_column(String(32), default="new")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    follow_up_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    search_job: Mapped[SearchJob] = relationship(back_populates="leads")
    candidate: Mapped[SearchCandidate | None] = relationship(back_populates="lead")


class SearchCandidate(Base):
    """Raw discovery pool. Written before any filtering so the pool survives
    failures in later stages and rejections stay auditable."""

    __tablename__ = "search_candidates"
    __table_args__ = (
        Index("uq_candidate_search_identity", "search_job_id", "identity_key", unique=True),
        Index("ix_search_candidates_job_status", "search_job_id", "status"),
        Index("ix_search_candidates_job_score", "search_job_id", "relevance_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_job_id: Mapped[int] = mapped_column(ForeignKey("search_jobs.id"), index=True)
    identity_key: Mapped[str] = mapped_column(String(64))

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url_normalized: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=CandidateStatus.DISCOVERED.value)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    filter_match: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    search_job: Mapped[SearchJob] = relationship(back_populates="candidates")
    lead: Mapped[LeadRecord | None] = relationship(back_populates="candidate", uselist=False)


class SearchQueryRun(Base):
    """One row per executed search-engine query, so a single failing query is
    recorded rather than failing the whole search."""

    __tablename__ = "search_query_runs"
    __table_args__ = (
        Index("ix_query_runs_job_status", "search_job_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_job_id: Mapped[int] = mapped_column(ForeignKey("search_jobs.id"), index=True)
    query_text: Mapped[str] = mapped_column(Text)
    query_hash: Mapped[str] = mapped_column(String(64), index=True)
    dimension: Mapped[str | None] = mapped_column(String(64), nullable=True)
    page: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default=QueryRunStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    search_job: Mapped[SearchJob] = relationship(back_populates="query_runs")


class SearchQueryCache(Base):
    """Cached raw SERP hits for a public query.

    Keyed only by the query text and page — never by user or organization — so it
    holds nothing user-specific. Qualification and scoring always re-run against
    the requesting user's own prompt and filters.
    """

    __tablename__ = "search_query_cache"
    __table_args__ = (Index("ix_query_cache_expires", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    query_text: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer, default=1)
    hits: Mapped[list] = mapped_column(JSON, default=list)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QueuedJob(Base):
    """Durable work queue. Replaces the daemon threads that were lost on restart."""

    __tablename__ = "job_queue"
    __table_args__ = (
        Index("ix_job_queue_claim", "status", "available_at"),
        Index("ix_job_queue_type_status", "job_type", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=QueuedJobStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


class LeadNote(Base):
    __tablename__ = "lead_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("lead_records.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LeadTask(Base):
    __tablename__ = "lead_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("lead_records.id"), index=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LeadActivity(Base):
    __tablename__ = "lead_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("lead_records.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CrmContact(Base):
    """Selective CRM database — distinct from discovered LeadRecord rows."""

    __tablename__ = "crm_contacts"
    __table_args__ = (
        Index("ix_crm_contacts_owner", "owner_id", "organization_id"),
        Index("ix_crm_contacts_identity", "owner_id", "organization_id", "identity_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("lead_records.id"), nullable=True, index=True)
    identity_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    email_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    stage: Mapped[str] = mapped_column(String(32), default="new")
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CrmGroup(Base):
    """Nested CRM groups and subgroups (static or dynamic/smart)."""

    __tablename__ = "crm_groups"
    __table_args__ = (
        Index("ix_crm_groups_owner", "owner_id", "organization_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("crm_groups.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="static")  # static | smart
    smart_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CrmGroupMembership(Base):
    __tablename__ = "crm_group_memberships"
    __table_args__ = (
        UniqueConstraint("group_id", "contact_id", name="uq_crm_group_contact"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("crm_groups.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("crm_contacts.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CrmExportRecord(Base):
    __tablename__ = "crm_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))  # crm_transfer | csv | xml | json | external_crm
    destination: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    transferred_count: Mapped[int] = mapped_column(Integer, default=0)
    with_email_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    field_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SavedFilterPreset(Base):
    __tablename__ = "saved_filter_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    scope: Mapped[str] = mapped_column(String(32), default="leads")  # leads | search | crm
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AutomationRule(Base):
    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    trigger: Mapped[str] = mapped_column(String(64))  # email_verified | search_completed
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)
    actions: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SchemaMigration(Base):
    """Applied schema versions. ``create_all`` does not record these."""

    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GlobalCompany(Base):
    """Shared company evidence. Not a workspace account and not a CRM company."""

    __tablename__ = "global_companies"
    __table_args__ = (
        Index("ix_global_companies_name", "name_normalized"),
        Index("ix_global_companies_country", "country_code"),
        Index("ix_global_companies_industry", "industry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subindustry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_band: Mapped[str | None] = mapped_column(String(32), nullable=True)
    company_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    provider_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GlobalPerson(Base):
    """Shared public prospect. Workspace notes, tags, and CRM stay on ``lead_records``."""

    __tablename__ = "global_people"
    __table_args__ = (
        Index("ix_global_people_country", "country_code"),
        Index("ix_global_people_industry", "industry"),
        Index("ix_global_people_domain", "domain"),
        Index("ix_global_people_email_status", "email_status"),
        Index("ix_global_people_seniority", "seniority"),
        Index("ix_global_people_function", "job_function"),
        Index("ix_global_people_tier_updated", "quality_tier", "updated_at"),
        Index("ix_global_people_name", "name_normalized"),
        Index("ix_global_people_profile", "profile_url_normalized"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    given_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    family_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_function: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("global_companies.id"), nullable=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_name_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subindustry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_band: Mapped[str | None] = mapped_column(String(32), nullable=True)
    company_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_url_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    quality_tier: Mapped[str] = mapped_column(String(32), default="basic")
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    company: Mapped[GlobalCompany | None] = relationship()


class GlobalPersonTechnology(Base):
    __tablename__ = "global_person_technologies"
    __table_args__ = (
        Index("uq_global_person_tech", "person_id", "technology", unique=True),
        Index("ix_global_person_tech_name", "technology"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("global_people.id"), index=True)
    technology: Mapped[str] = mapped_column(String(64))


class GlobalEvidence(Base):
    """Provenance for one field. Ineligible rows are stored but never served."""

    __tablename__ = "global_evidence"
    __table_args__ = (
        Index("ix_global_evidence_subject", "subject_type", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[int] = mapped_column(Integer)
    field_name: Mapped[str] = mapped_column(String(64))
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    share_scope: Mapped[str] = mapped_column(String(32), default="public")
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GlobalIdentityKey(Base):
    __tablename__ = "global_identity_keys"
    __table_args__ = (
        Index("uq_global_identity_key", "kind", "key_value", unique=True),
        Index("ix_global_identity_subject", "subject_type", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key_value: Mapped[str] = mapped_column(String(255))
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[int] = mapped_column(Integer)


class SuppressionEntry(Base):
    """Identities the shared cache must not store or serve."""

    __tablename__ = "suppression_entries"
    __table_args__ = (
        Index("uq_suppression_kind_value", "kind", "normalized_value", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    normalized_value: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DiscoverySettings(Base):
    """One row. Discovery and paid calls default off."""

    __tablename__ = "discovery_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    live_public_search_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_providers_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    max_runs_per_tick: Mapped[int] = mapped_column(Integer, default=5)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DiscoveryTarget(Base):
    """Saved crawl target. Scheduling state is not the same thing as cancelling a run."""

    __tablename__ = "discovery_targets"
    __table_args__ = (Index("ix_discovery_targets_due", "scheduling_status", "next_run_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    #: active | paused | stopped. Stopped means no new runs. Queued runs stay.
    scheduling_status: Mapped[str] = mapped_column(String(32), default="paused")
    #: interval | daily | weekdays
    schedule_type: Mapped[str] = mapped_column(String(32), default="daily")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hour: Mapped[int] = mapped_column(Integer, default=6)
    minute: Mapped[int] = mapped_column(Integer, default=0)
    max_people_per_run: Mapped[int] = mapped_column(Integer, default=25)
    max_queries_per_run: Mapped[int] = mapped_column(Integer, default=5)
    refresh_hours: Mapped[int] = mapped_column(Integer, default=168)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    industries: Mapped[list] = mapped_column(JSON, default=list)
    titles: Mapped[list] = mapped_column(JSON, default=list)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (
        Index("uq_discovery_run_idempotency", "idempotency_key", unique=True),
        Index("ix_discovery_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("discovery_targets.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    people_limit: Mapped[int] = mapped_column(Integer, default=25)
    query_limit: Mapped[int] = mapped_column(Integer, default=5)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    queue_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DiscoveryStageAttempt(Base):
    __tablename__ = "discovery_stage_attempts"
    __table_args__ = (Index("ix_discovery_stage_run", "run_id", "stage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("discovery_runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SchedulerLock(Base):
    """Lease so two processes do not plan the same tick. Not a substitute for SKIP LOCKED."""

    __tablename__ = "scheduler_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CachePolicy(Base):
    """One row of quality rules. Tiers are labels, not accuracy guarantees."""

    __tablename__ = "cache_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="default")
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

