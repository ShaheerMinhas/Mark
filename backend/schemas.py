from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from backend.models import AccountType, OrgRole, SearchJobStatus


def _csv_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_csv_list(item))
        return out
    return [str(value).strip()] if str(value).strip() else []


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1)
    account_type: AccountType = AccountType.INDIVIDUAL
    organization_name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    account_type: AccountType
    organizations: list["OrgMembershipOut"] = []

    model_config = {"from_attributes": True}


class OrgMembershipOut(BaseModel):
    organization_id: int
    organization_name: str
    role: OrgRole

    model_config = {"from_attributes": True}


class OrganizationOut(BaseModel):
    id: int
    name: str
    slug: str
    member_count: int = 0

    model_config = {"from_attributes": True}


class InviteMemberRequest(BaseModel):
    email: EmailStr
    role: OrgRole = OrgRole.MEMBER


class LinkedInSessionCreate(BaseModel):
    session_cookie: str = Field(min_length=10, description="LinkedIn li_at cookie value")
    label: str = "Primary"


class LinkedInSessionOut(BaseModel):
    id: int
    label: str
    linkedin_name: Optional[str]
    is_valid: bool
    last_validated_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class SearchFilters(BaseModel):
    location: str = ""
    country: str = ""
    state_region: str = ""
    city: str = ""
    industry: str = ""
    company: str = ""
    company_location: str = ""
    interests: list[str] = Field(default_factory=list)
    job_titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    company_size: str = ""
    income_level: str = ""
    gender: str = ""
    seniority: str = ""
    age_min: Optional[int] = Field(default=25, ge=13, le=100)
    age_max: Optional[int] = Field(default=45, ge=13, le=100)
    language: str = ""
    max_results: int = Field(default=2000, ge=10, le=10000)
    active_only: bool = True
    business_only: bool = True
    #: Email enrichment timing: "selected" (default), "after_discovery", "auto"
    enrichment_mode: str = "after_discovery"
    max_api_credits: Optional[int] = None
    max_external_requests: Optional[int] = None
    max_duration_seconds: Optional[int] = None

    @field_validator("job_titles", "interests", "keywords", "technologies", mode="before")
    @classmethod
    def split_list_fields(cls, value: Any) -> list[str]:
        return _csv_list(value)


class SearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    prompt: str = Field(min_length=1)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    organization_id: Optional[int] = None
    save_results: bool = True


class LeadOut(BaseModel):
    id: Optional[int] = None
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    phone_source: Optional[str] = None
    personal_location: Optional[str] = None
    ai_description: Optional[str] = None
    work_field: Optional[str] = None
    work_designation: Optional[str] = None
    company_name: Optional[str] = None
    company_location: Optional[str] = None
    linkedin_url: Optional[str] = None
    relevance_score: float = 0.0
    activity_status: Optional[str] = None
    raw_snippet: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    company_domain: Optional[str] = None
    email_status: Optional[str] = None
    email_confidence: Optional[str] = None
    email_source: Optional[str] = None
    email_pattern: Optional[str] = None
    email_source_url: Optional[str] = None
    email_evidence: Optional[list[dict[str, Any]]] = None
    email_verification: Optional[dict[str, Any]] = None
    email_verified_at: Optional[datetime] = None
    email_enriched_at: Optional[datetime] = None
    crm_stage: str = "new"
    crm_notes: Optional[str] = None
    lead_status: str = "new"
    priority: str = "medium"
    assigned_user_id: Optional[int] = None
    follow_up_date: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    score_breakdown: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SearchJobOut(BaseModel):
    id: int
    name: str
    prompt: str
    filters: dict[str, Any]
    linkedin_url: Optional[str]
    query_used: Optional[str]
    mode: str
    #: Collapsed to pending/running/completed/failed for existing clients.
    status: SearchJobStatus
    #: The granular pipeline status (discovering, filtering, enriching, …).
    detailed_status: Optional[SearchJobStatus] = None
    stage: Optional[str] = None
    message: Optional[str]
    progress: int = 0
    found_count: int = 0
    lead_count: int = 0
    candidates_found: int = 0
    qualified_leads: int = 0
    emails_found: int = 0
    phones_found: int = 0
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EmailLookupOut(BaseModel):
    """Whether this search is looking up emails, and how far that pass got."""

    enabled: bool = False
    mode: str = "after_discovery"
    state: str = "not_requested"
    source: str = "none"
    reason: str = ""
    processed: int = 0
    total: int = 0
    found: int = 0
    detail: str = ""


class RunStageOut(BaseModel):
    key: str
    label: str
    state: str


class SearchRunOut(BaseModel):
    """One progress story for discovery, qualification, and email lookup."""

    active: bool = False
    stage: str = ""
    stage_label: str = ""
    message: str = ""
    percent: Optional[int] = None
    determinate: bool = False
    email_lookup: EmailLookupOut = Field(default_factory=EmailLookupOut)
    stages: list[RunStageOut] = Field(default_factory=list)


class SearchProgressOut(BaseModel):
    """Everything the frontend needs to render progress, with no lead payload."""

    id: int
    name: str
    status: SearchJobStatus
    legacy_status: SearchJobStatus
    current_stage: Optional[str] = None
    progress: int = 0
    message: str = ""
    error_message: Optional[str] = None
    cancel_requested: bool = False
    run: Optional[SearchRunOut] = None

    total_queries: int = 0
    completed_queries: int = 0
    failed_queries: int = 0
    candidates_found: int = 0
    candidates_deduplicated: int = 0
    qualified_leads: int = 0
    enrichment_attempted: int = 0
    enrichment_completed: int = 0
    emails_found: int = 0
    phones_found: int = 0
    errors_count: int = 0
    lead_count: int = 0

    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool


class LeadPageOut(BaseModel):
    items: list[LeadOut]
    page: PageMeta


class PeopleItem(LeadOut):
    search_id: int
    search_name: str = ""


class PeoplePageOut(BaseModel):
    items: list[PeopleItem]
    page: PageMeta
    stage_counts: dict[str, int] = Field(default_factory=dict)
    #: Counts for the current non-email filters, so email chips stay comparable.
    email_counts: dict[str, int] = Field(default_factory=dict)
    #: Leads in this view before text, stage, company, and email filters.
    library_total: int = 0


class PeopleUpdate(BaseModel):
    crm_stage: Optional[str] = None
    crm_notes: Optional[str] = None
    lead_status: Optional[str] = None
    priority: Optional[str] = None
    assigned_user_id: Optional[int] = None
    follow_up_date: Optional[datetime] = None
    archived: Optional[bool] = None
    tags: Optional[list[str]] = None
    custom_fields: Optional[dict[str, Any]] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    work_designation: Optional[str] = None
    company_name: Optional[str] = None
    personal_location: Optional[str] = None


class CandidateOut(BaseModel):
    id: int
    name: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    query_text: Optional[str] = None
    status: str
    relevance_score: float = 0.0
    rejection_reason: Optional[str] = None
    discovered_at: datetime

    model_config = {"from_attributes": True}


class CandidatePageOut(BaseModel):
    items: list[CandidateOut]
    page: PageMeta


class SearchFunnelOut(BaseModel):
    """The counts that must stay distinguishable.

    Relevant leads include people with only a LinkedIn URL. Contactable is the
    subset that also has an email — it is not a gate on appearing in results.
    """

    candidates_found: int = 0
    relevant_leads: int = 0
    linkedin_leads: int = 0
    enriched_leads: int = 0
    contactable_leads: int = 0
    phoneable_leads: int = 0
    rejections: dict[str, int] = Field(default_factory=dict)
    query_outcomes: dict[str, int] = Field(default_factory=dict)


class EmailJobOut(BaseModel):
    id: int
    search_job_id: int
    status: str
    progress: int = 0
    message: Optional[str] = None
    processed: int = 0
    total: int = 0
    found: int = 0
    inferred: int = 0
    verified: int = 0
    not_found: int = 0

    model_config = {"from_attributes": True}


class SearchResultOut(BaseModel):
    search: SearchJobOut
    leads: list[LeadOut]
    query_used: str
    count: int
    message: str
    mode: str
    email_job: Optional[EmailJobOut] = None
