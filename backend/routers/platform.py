"""Platform analytics, job monitoring, integrations, presets, automations, and notifications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import config
from backend.database import get_db
from backend.dependencies import get_current_user, require_org_access
from backend.models import (
    AutomationRule,
    CrmContact,
    CrmExportRecord,
    CrmGroup,
    EnrichmentStatus,
    LeadActivity,
    LeadRecord,
    Notification,
    OPEN_SEARCH_STATUSES,
    ProviderUsage,
    QueuedJob,
    SavedFilterPreset,
    SearchJob,
    SearchJobStatus,
    User,
)
from backend.services.search_engines.dispatcher import engine_health

router = APIRouter(prefix="/api/platform", tags=["platform"])


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scope: str = "leads"
    filters: dict[str, Any] = Field(default_factory=dict)
    organization_id: int | None = None


class AutomationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    trigger: str = "email_verified"  # email_verified | search_completed
    conditions: dict[str, Any] = Field(default_factory=dict)
    actions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    organization_id: int | None = None


def _scope_jobs(db: Session, user: User, org_id: int | None):
    q = db.query(SearchJob)
    if org_id is not None:
        require_org_access(db, user, org_id)
        return q.filter(SearchJob.organization_id == org_id)
    return q.filter(SearchJob.owner_id == user.id)


@router.get("/dashboard")
def get_dashboard_overview(
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Real database figures only — no synthetic stats."""
    jobs_q = _scope_jobs(db, user, organization_id)
    job_ids = [jid for (jid,) in jobs_q.with_entities(SearchJob.id).all()]

    leads_q = db.query(LeadRecord).filter(LeadRecord.search_job_id.in_(job_ids or [-1]))
    total_leads = leads_q.count()
    unique_people = (
        db.query(func.count(func.distinct(func.coalesce(LeadRecord.identity_key, LeadRecord.name))))
        .filter(LeadRecord.search_job_id.in_(job_ids or [-1]))
        .scalar()
        or 0
    )
    confirmed_emails = (
        leads_q.filter(LeadRecord.email.isnot(None), LeadRecord.email != "").count()
    )
    verified_emails = (
        leads_q.filter(LeadRecord.email_status == "verified").count()
    )
    pending_enrichment = (
        leads_q.filter(LeadRecord.enrichment_status == EnrichmentStatus.PENDING.value).count()
    )

    all_jobs = jobs_q.order_by(SearchJob.created_at.desc()).all()
    active_searches = sum(1 for j in all_jobs if j.status in OPEN_SEARCH_STATUSES)
    completed_searches = sum(
        1
        for j in all_jobs
        if j.status in {SearchJobStatus.COMPLETED, SearchJobStatus.PARTIALLY_COMPLETED}
    )

    crm_q = db.query(CrmContact)
    groups_q = db.query(CrmGroup)
    if organization_id is not None:
        crm_q = crm_q.filter(CrmContact.organization_id == organization_id)
        groups_q = groups_q.filter(CrmGroup.organization_id == organization_id)
    else:
        crm_q = crm_q.filter(CrmContact.owner_id == user.id, CrmContact.organization_id.is_(None))
        groups_q = groups_q.filter(CrmGroup.owner_id == user.id, CrmGroup.organization_id.is_(None))

    crm_contacts = crm_q.count()
    crm_groups = groups_q.count()

    usage_rows = (
        db.query(
            ProviderUsage.provider,
            func.count(ProviderUsage.id),
            func.coalesce(func.sum(ProviderUsage.credits), 0),
        )
        .filter(ProviderUsage.search_id.in_(job_ids or [-1]))
        .group_by(ProviderUsage.provider)
        .all()
    )
    api_usage = [
        {"provider": p, "requests": int(r), "credits": int(c)}
        for p, r, c in usage_rows
    ]
    total_credits = sum(item["credits"] for item in api_usage)

    recent_leads = (
        leads_q.order_by(LeadRecord.id.desc())
        .limit(8)
        .all()
    )
    recent_activities = (
        db.query(LeadActivity)
        .join(LeadRecord, LeadActivity.lead_id == LeadRecord.id)
        .filter(LeadRecord.search_job_id.in_(job_ids or [-1]))
        .order_by(LeadActivity.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "total_leads_discovered": int(total_leads),
        "unique_people": int(unique_people),
        "confirmed_emails": int(confirmed_emails),
        "verified_emails": int(verified_emails),
        "pending_email_enrichment": int(pending_enrichment),
        "active_searches": int(active_searches),
        "completed_searches": int(completed_searches),
        "total_searches": len(all_jobs),
        "crm_contacts": int(crm_contacts),
        "crm_groups": int(crm_groups),
        "total_credits_used": int(total_credits),
        "api_usage": api_usage,
        "recent_leads": [
            {
                "id": l.id,
                "name": l.name,
                "title": l.work_designation,
                "company_name": l.company_name,
                "email": l.email,
                "lead_status": l.lead_status or l.crm_stage or "new",
            }
            for l in recent_leads
        ],
        "recent_activity": [
            {
                "id": a.id,
                "lead_id": a.lead_id,
                "event_type": a.event_type,
                "detail": a.detail,
                "created_at": a.created_at,
            }
            for a in recent_activities
        ],
    }


@router.get("/jobs")
def list_system_jobs(
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Job monitoring dashboard + search engine health."""
    jobs = (
        _scope_jobs(db, user, organization_id)
        .order_by(SearchJob.created_at.desc())
        .limit(50)
        .all()
    )
    queue_counts = dict(
        db.query(QueuedJob.status, func.count(QueuedJob.id))
        .group_by(QueuedJob.status)
        .all()
    )
    return {
        "search_jobs": [
            {
                "id": j.id,
                "name": j.name,
                "status": j.status.value if hasattr(j.status, "value") else str(j.status),
                "current_stage": j.current_stage,
                "progress": j.progress,
                "candidates_found": j.candidates_found,
                "qualified_leads": j.qualified_leads,
                "emails_found": j.emails_found,
                "total_queries": j.total_queries,
                "completed_queries": j.completed_queries,
                "failed_queries": j.failed_queries,
                "error_message": j.error_message,
                "created_at": j.created_at,
                "completed_at": j.completed_at,
            }
            for j in jobs
        ],
        "queue_counts": {str(k): int(v) for k, v in queue_counts.items()},
        "concurrency": {
            "search_max_concurrency": config.SEARCH_MAX_CONCURRENCY,
            "enrichment_max_concurrency": config.ENRICHMENT_MAX_CONCURRENCY,
            "crawler_max_concurrency": config.CRAWLER_MAX_CONCURRENCY,
            "worker_concurrency": config.WORKER_CONCURRENCY,
            "batch_size": config.BATCH_SIZE,
        },
        "engine_health": engine_health(),
    }


@router.get("/integrations")
def list_integrations(user: User = Depends(get_current_user)):
    """Only actually implemented and configured providers are reported active."""
    email_providers = [
        {"id": "public_web", "name": "Public Web & SERP Harvest", "kind": "email", "configured": True, "free": True},
        {"id": "hunter", "name": "Hunter.io", "kind": "email", "configured": bool(config.HUNTER_API_KEY), "free": False},
        {"id": "tomba", "name": "Tomba", "kind": "email", "configured": bool(config.TOMBA_API_KEY and config.TOMBA_SECRET), "free": False},
        {"id": "leadmagic", "name": "LeadMagic", "kind": "email", "configured": bool(config.LEADMAGIC_API_KEY), "free": False},
        {"id": "prospeo", "name": "Prospeo", "kind": "email", "configured": bool(config.PROSPEO_API_KEY), "free": False},
        {"id": "findymail", "name": "Findymail", "kind": "email", "configured": bool(config.FINDYMAIL_API_KEY), "free": False},
    ]
    crms = [
        {
            "id": "mark_crm",
            "name": "Mark Internal CRM",
            "configured": True,
            "auto_sync": False,
            "status": "active",
        },
        {"id": "hubspot", "name": "HubSpot Connector", "configured": False, "auto_sync": False, "status": "not_configured"},
        {"id": "salesforce", "name": "Salesforce Connector", "configured": False, "auto_sync": False, "status": "not_configured"},
        {"id": "pipedrive", "name": "Pipedrive Connector", "configured": False, "auto_sync": False, "status": "not_configured"},
        {"id": "zoho", "name": "Zoho CRM Connector", "configured": False, "auto_sync": False, "status": "not_configured"},
    ]
    return {"email_providers": email_providers, "crm_connectors": crms}


@router.get("/presets")
def list_presets(
    scope: str = "",
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(SavedFilterPreset)
    if organization_id is not None:
        require_org_access(db, user, organization_id)
        q = q.filter(SavedFilterPreset.organization_id == organization_id)
    else:
        q = q.filter(SavedFilterPreset.owner_id == user.id)
    if scope:
        q = q.filter(SavedFilterPreset.scope == scope)
    rows = q.order_by(SavedFilterPreset.created_at.desc()).all()
    return {
        "items": [
            {"id": r.id, "name": r.name, "scope": r.scope, "filters": r.filters or {}, "created_at": r.created_at}
            for r in rows
        ]
    }


@router.post("/presets")
def create_preset(
    body: PresetCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.organization_id is not None:
        require_org_access(db, user, body.organization_id)
    row = SavedFilterPreset(
        owner_id=user.id,
        organization_id=body.organization_id,
        name=body.name.strip(),
        scope=body.scope.strip() or "leads",
        filters=body.filters or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "scope": row.scope, "filters": row.filters}


@router.delete("/presets/{preset_id}")
def delete_preset(
    preset_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(SavedFilterPreset).filter(SavedFilterPreset.id == preset_id, SavedFilterPreset.owner_id == user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Preset not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/automations")
def list_automations(
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(AutomationRule)
    if organization_id is not None:
        require_org_access(db, user, organization_id)
        q = q.filter(AutomationRule.organization_id == organization_id)
    else:
        q = q.filter(AutomationRule.owner_id == user.id)
    rows = q.order_by(AutomationRule.created_at.desc()).all()
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "trigger": r.trigger,
                "conditions": r.conditions or {},
                "actions": r.actions or {},
                "enabled": r.enabled,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.post("/automations")
def create_automation(
    body: AutomationCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.organization_id is not None:
        require_org_access(db, user, body.organization_id)
    # Never allow silent external CRM sync in automation actions.
    if body.actions.get("sync_external_crm"):
        raise HTTPException(status_code=400, detail="External CRM synchronization requires explicit manual selection")
    rule = AutomationRule(
        owner_id=user.id,
        organization_id=body.organization_id,
        name=body.name.strip(),
        trigger=body.trigger.strip(),
        conditions=body.conditions or {},
        actions=body.actions or {},
        enabled=body.enabled,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {
        "id": rule.id,
        "name": rule.name,
        "trigger": rule.trigger,
        "conditions": rule.conditions,
        "actions": rule.actions,
        "enabled": rule.enabled,
    }


@router.get("/notifications")
def list_notifications(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(30)
        .all()
    )
    return {
        "items": [
            {"id": n.id, "title": n.title, "body": n.body, "read": n.read, "created_at": n.created_at}
            for n in rows
        ]
    }
