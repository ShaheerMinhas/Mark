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
    industry: str = ""
    company: str = ""
    company_location: str = ""
    interests: list[str] = Field(default_factory=list)
    job_titles: list[str] = Field(default_factory=list)
    company_size: str = ""
    income_level: str = ""
    gender: str = ""
    seniority: str = ""
    age_min: Optional[int] = Field(default=25, ge=13, le=100)
    age_max: Optional[int] = Field(default=45, ge=13, le=100)
    language: str = ""
    max_results: int = Field(default=100, ge=10, le=250)
    active_only: bool = True
    business_only: bool = True

    @field_validator("job_titles", "interests", mode="before")
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

    model_config = {"from_attributes": True}


class SearchJobOut(BaseModel):
    id: int
    name: str
    prompt: str
    filters: dict[str, Any]
    linkedin_url: Optional[str]
    query_used: Optional[str]
    mode: str
    status: SearchJobStatus
    message: Optional[str]
    progress: int = 0
    found_count: int = 0
    lead_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


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
