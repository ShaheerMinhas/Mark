"""Platform-operator controls for global discovery. No customer can call these."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import require_platform_admin
from backend.models import DiscoveryCategory, DiscoveryFieldRule, DiscoveryRun, DiscoveryTarget, GlobalPerson, User
from backend.services.discovery_engine.admin_read import (
    archive_company,
    archive_person,
    correct_person,
    list_companies,
    list_people,
    list_runs,
    overview,
    people_csv,
    person_detail,
    run_detail,
    _company_dict,
    _person_dict,
    _run_dict,
)
from backend.services.discovery_engine.schedule import enqueue_run_now, validate_timezone
from backend.services.discovery_engine.targeting import apply_criteria, field_rules, preferred_as_required, validate_criteria
from backend.services.discovery_engine.settings import ensure_settings, update_limits
from backend.services.discovery_engine.suppression import add_suppression

router = APIRouter(prefix="/api/platform/discovery", tags=["discovery-control"])


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    priority: int = 100
    schedule_type: str = "daily"
    timezone: str = "UTC"
    interval_minutes: int | None = None
    hour: int = 6
    minute: int = 0
    max_people_per_run: int = 25
    max_queries_per_run: int = 5
    refresh_hours: int = 168
    countries: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    criteria: dict | None = None
    autonomous: bool = False


class SuppressionIn(BaseModel):
    kind: str
    value: str
    reason: str | None = None


def _check_target(body: TargetIn) -> None:
    if body.schedule_type not in {"interval", "hourly", "daily", "weekdays"}:
        raise HTTPException(status_code=400, detail="schedule_type must be interval, hourly, daily, or weekdays")
    try:
        validate_timezone(body.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not 0 <= body.hour <= 23 or not 0 <= body.minute <= 59:
        raise HTTPException(status_code=400, detail="hour or minute is out of range")
    if body.schedule_type == "interval" and (body.interval_minutes or 0) < 15:
        raise HTTPException(status_code=400, detail="interval_minutes must be at least 15")
    if not 1 <= body.max_people_per_run <= 500:
        raise HTTPException(status_code=400, detail="max_people_per_run must be 1..500")
    if not 1 <= body.max_queries_per_run <= 50:
        raise HTTPException(status_code=400, detail="max_queries_per_run must be 1..50")
    if not 1 <= body.refresh_hours <= 24 * 90:
        raise HTTPException(status_code=400, detail="refresh_hours is out of range")


def _target_out(row: DiscoveryTarget) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "priority": row.priority,
        "scheduling_status": row.scheduling_status,
        "schedule_type": row.schedule_type,
        "timezone": row.timezone,
        "interval_minutes": row.interval_minutes,
        "hour": row.hour,
        "minute": row.minute,
        "max_people_per_run": row.max_people_per_run,
        "max_queries_per_run": row.max_queries_per_run,
        "refresh_hours": row.refresh_hours,
        "countries": row.countries or [],
        "industries": row.industries or [],
        "titles": row.titles or [],
        "criteria": row.criteria or {},
        "autonomous": bool(getattr(row, "autonomous", False)),
        "rotation_cursor": int(getattr(row, "rotation_cursor", 0) or 0),
        "last_query": row.last_query,
        "next_run_at": row.next_run_at,
    }


@router.post("/targets")
def create_target(
    body: TargetIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    _check_target(body)
    criteria = {}
    if body.criteria:
        try:
            criteria = validate_criteria(db, body.criteria)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = DiscoveryTarget(
        name=body.name.strip(),
        priority=body.priority,
        scheduling_status="paused",
        schedule_type="interval" if body.autonomous else body.schedule_type,
        timezone=body.timezone,
        interval_minutes=120 if body.autonomous else body.interval_minutes,
        autonomous=bool(body.autonomous),
        hour=body.hour,
        minute=body.minute,
        max_people_per_run=body.max_people_per_run,
        max_queries_per_run=body.max_queries_per_run,
        refresh_hours=body.refresh_hours,
        countries=(criteria.get("geography") or {}).get("include_countries") or body.countries,
        industries=(criteria.get("industry") or {}).get("include") or body.industries,
        titles=(criteria.get("titles") or {}).get("exact") or body.titles,
        criteria=criteria,
        next_run_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _target_out(row)


def _set_scheduling(db: Session, target_id: int, status: str) -> dict:
    row = db.get(DiscoveryTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    row.scheduling_status = status
    row.updated_at = datetime.utcnow()
    if status == "active" and row.next_run_at is None:
        row.next_run_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _target_out(row)


class FieldRuleIn(BaseModel):
    collection_enabled: bool = True
    requirement: str = "optional"
    priority: int = 100
    refresh_hours: int = 168
    verification_required: bool = False
    enrichment_source: str | None = None


class CategoryIn(BaseModel):
    kind: str
    name: str


@router.patch("/targets/{target_id}")
def update_target(
    target_id: int,
    body: TargetIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    row = db.get(DiscoveryTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    _check_target(body)
    try:
        criteria = validate_criteria(db, body.criteria or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.name = body.name.strip()
    row.priority = body.priority
    row.autonomous = bool(body.autonomous)
    row.schedule_type = "interval" if body.autonomous else body.schedule_type
    row.interval_minutes = 120 if body.autonomous else body.interval_minutes
    row.timezone = body.timezone
    row.hour = body.hour
    row.minute = body.minute
    row.max_people_per_run = body.max_people_per_run
    row.max_queries_per_run = body.max_queries_per_run
    row.refresh_hours = body.refresh_hours
    row.countries = criteria["geography"]["include_countries"] or body.countries
    row.industries = criteria["industry"]["include"] or body.industries
    row.titles = criteria["titles"]["exact"] or body.titles
    row.criteria = criteria
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _target_out(row)


@router.post("/targets/{target_id}/preview")
def preview_target(target_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    row = db.get(DiscoveryTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    from backend.services.discovery_engine.suppression import suppressed_people_filter

    rules = field_rules(db)
    def base():
        return suppressed_people_filter(db, db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None)))

    criteria = dict(row.criteria or {})
    eligible = apply_criteria(db, base(), criteria, rules).count()
    preferred = apply_criteria(db, base(), criteria, preferred_as_required(rules)).count()
    return {
        "eligible": eligible,
        "preferred_complete": preferred,
        "detail": "Eligible means required fields match. A missing optional field stays eligible. Rows that do not match are left in the cache.",
        "enrichment": "unavailable",
    }


@router.get("/catalogue")
def list_catalogue(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    rows = db.query(DiscoveryCategory).filter(DiscoveryCategory.active.is_(True)).order_by(DiscoveryCategory.kind, DiscoveryCategory.name).all()
    return {
        "items": [
            {"id": row.id, "kind": row.kind, "name": row.name, "normalized": row.normalized}
            for row in rows
        ]
    }


@router.post("/catalogue")
def add_catalogue(
    body: CategoryIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    if body.kind not in {"industry", "subindustry", "technology", "function", "seniority", "department"}:
        raise HTTPException(status_code=400, detail="Unsupported catalogue kind")
    from backend.services.identity import normalize_name

    normalized = normalize_name(body.name)
    if not normalized:
        raise HTTPException(status_code=400, detail="Category name is empty")
    existing = (
        db.query(DiscoveryCategory)
        .filter(DiscoveryCategory.kind == body.kind, DiscoveryCategory.normalized == normalized)
        .one_or_none()
    )
    if existing is None:
        existing = DiscoveryCategory(kind=body.kind, name=body.name.strip(), normalized=normalized, active=True)
        db.add(existing)
        db.commit()
        db.refresh(existing)
    return {"id": existing.id, "kind": existing.kind, "name": existing.name}


@router.get("/fields")
def list_fields(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    rows = db.query(DiscoveryFieldRule).order_by(DiscoveryFieldRule.priority.asc()).all()
    return {
        "enrichment_application": "unavailable",
        "detail": "Collection and required/preferred/optional are used when matching. Email find and verify run only through the paid gates. Other fields have no adapter.",
        "items": [
            {
                "field_name": row.field_name,
                "collection_enabled": row.collection_enabled,
                "requirement": row.requirement,
                "priority": row.priority,
                "refresh_hours": row.refresh_hours,
                "verification_required": row.verification_required,
                "enrichment_source": row.enrichment_source if row.field_name == "email" else None,
                "enrichment_source_status": "gated" if row.field_name == "email" else "unavailable",
            }
            for row in rows
        ],
    }


@router.patch("/fields/{field_name}")
def update_field(
    field_name: str,
    body: FieldRuleIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    if body.requirement not in {"required", "preferred", "optional"}:
        raise HTTPException(status_code=400, detail="requirement must be required, preferred, or optional")
    if not 1 <= body.refresh_hours <= 24 * 90:
        raise HTTPException(status_code=400, detail="refresh_hours is out of range")
    row = db.query(DiscoveryFieldRule).filter(DiscoveryFieldRule.field_name == field_name).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown field")
    row.collection_enabled = body.collection_enabled
    row.requirement = body.requirement
    row.priority = body.priority
    row.refresh_hours = body.refresh_hours
    row.verification_required = body.verification_required
    if body.enrichment_source:
        from backend.services.discovery_engine.spend import PROVIDERS

        if field_name != "email":
            raise HTTPException(status_code=400, detail="No enrichment adapter for this field")
        if body.enrichment_source not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")
        row.enrichment_source = body.enrichment_source
    else:
        row.enrichment_source = None
    db.commit()
    status = "gated" if field_name == "email" else "unavailable"
    return {"field_name": row.field_name, "requirement": row.requirement, "enrichment_source_status": status}


@router.post("/targets/{target_id}/start")
def start_target(target_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    return _set_scheduling(db, target_id, "active")


@router.post("/targets/{target_id}/pause")
def pause_target(target_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    return _set_scheduling(db, target_id, "paused")


@router.post("/targets/{target_id}/resume")
def resume_target(target_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    return _set_scheduling(db, target_id, "active")


@router.post("/targets/{target_id}/stop")
def stop_target(target_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    return _set_scheduling(db, target_id, "stopped")


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run.cancel_requested = True
    if run.status == "queued":
        run.status = "cancelled"
        run.finished_at = datetime.utcnow()
    db.commit()
    return {"id": run.id, "status": run.status, "cancel_requested": True}


@router.post("/suppression")
def create_suppression(
    body: SuppressionIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    try:
        row = add_suppression(db, kind=body.kind, value=body.value, reason=body.reason, user_id=admin.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": row.id, "kind": row.kind, "normalized_value": row.normalized_value}


class LimitsIn(BaseModel):
    enabled: bool = False
    max_concurrency: int = 1
    max_runs_per_tick: int = 5
    max_runs_per_hour: int = 4
    max_runs_per_day: int = 24
    store_name_only: bool = True
    serve_name_only: bool = True


class CorrectionIn(BaseModel):
    title: str | None = None
    company_name: str | None = None
    city: str | None = None
    industry: str | None = None
    country_code: str | None = None
    note: str | None = None


def _settings_out(row) -> dict:
    return {
        "enabled": bool(row.enabled),
        "live_public_search_enabled": False,
        "paid_providers_enabled": bool(row.paid_providers_enabled),
        "pause_enrichment": bool(getattr(row, "pause_enrichment", True)),
        "pause_email": bool(getattr(row, "pause_email", True)),
        "pause_verify": bool(getattr(row, "pause_verify", True)),
        "max_concurrency": row.max_concurrency,
        "max_runs_per_tick": row.max_runs_per_tick,
        "max_runs_per_hour": row.max_runs_per_hour,
        "max_runs_per_day": row.max_runs_per_day,
        "store_name_only": bool(getattr(row, "store_name_only", True)),
        "serve_name_only": bool(getattr(row, "serve_name_only", True)),
        "detail": "Scheduling is off until this switch is saved on. An autonomous target then uses only html.duckduckgo.com. Contact enrichment reads the company site first. Paid email runs only when that switch is on, the channel is not paused, and a reservation succeeds.",
    }


@router.get("/overview")
def discovery_overview(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    return overview(db)


@router.get("/targets")
def list_targets(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    rows = db.query(DiscoveryTarget).order_by(DiscoveryTarget.priority.asc(), DiscoveryTarget.id.asc()).all()
    return {"items": [_target_out(row) for row in rows]}


@router.post("/targets/{target_id}/run-now")
def run_target_now(target_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    row = db.get(DiscoveryTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    try:
        run = enqueue_run_now(db, row)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": run.id, "status": run.status, "detail": "Queued. This build will not call live search or paid providers."}


@router.get("/runs")
def get_runs(
    status: str = "",
    sort: str = "newest",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    total, rows = list_runs(db, status=status, page=page, page_size=page_size, sort=sort)
    items = []
    for run in rows:
        target = db.get(DiscoveryTarget, run.target_id)
        items.append(_run_dict(run, target.name if target else ""))
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/runs/{run_id}")
def get_run(run_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    body = run_detail(db, run_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return body


@router.get("/people")
def admin_people(
    q: str = "",
    country: str = "",
    tier: str = "",
    include_archived: bool = False,
    sort: str = "updated",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    total, rows = list_people(
        db, q=q, country=country, tier=tier, include_archived=include_archived, sort=sort, page=page, page_size=page_size
    )
    return {"total": total, "page": page, "page_size": page_size, "items": [_person_dict(row) for row in rows]}


@router.get("/people/export")
def export_people(
    q: str = "",
    country: str = "",
    tier: str = "",
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    _total, rows = list_people(
        db, q=q, country=country, tier=tier, include_archived=False, sort="name", page=1, page_size=100
    )
    return Response(
        content=people_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cached-people.csv"},
    )


@router.get("/people/{person_id}")
def admin_person(person_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    body = person_detail(db, person_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return body


@router.post("/people/{person_id}/retain")
def retain_cached_person(person_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    from backend.services.discovery_engine.suppression import suppress_and_archive

    row = suppress_and_archive(db, person_id, user_id=admin.id, reason="operator retention")
    if row is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return _person_dict(row)


@router.post("/retention/apply")
def run_retention(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    from backend.services.discovery_engine.suppression import apply_retention

    return {"archived": apply_retention(db), "compliance": "not_a_gdpr_deletion"}


@router.post("/people/{person_id}/archive")
def archive_cached_person(person_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    row = archive_person(db, person_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return _person_dict(row)


@router.post("/people/{person_id}/correct")
def correct_cached_person(
    person_id: int,
    body: CorrectionIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    fields = body.model_dump(exclude={"note"})
    try:
        row = correct_person(db, person_id, fields, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return _person_dict(row)


@router.get("/companies")
def admin_companies(
    q: str = "",
    country: str = "",
    include_archived: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    total, rows = list_companies(db, q=q, country=country, include_archived=include_archived, page=page, page_size=page_size)
    return {"total": total, "page": page, "page_size": page_size, "items": [_company_dict(row) for row in rows]}


@router.post("/companies/{company_id}/archive")
def archive_cached_company(company_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    row = archive_company(db, company_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return _company_dict(row)


class SearchBackendIn(BaseModel):
    enabled: bool | None = None
    max_requests_per_run: int | None = None
    daily_cap: int | None = None
    api_key: str | None = None
    cx: str | None = None


@router.get("/search-backends")
def get_search_backends(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    from backend.services.discovery_engine.search_backends import search_backend_report

    return {"items": search_backend_report(db, commit=True)}


@router.patch("/search-backends/{name}")
def patch_search_backend(
    name: str,
    body: SearchBackendIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    from backend.services.discovery_engine.search_backends import search_backend_report, update_search_backend

    try:
        update_search_backend(db, name, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": search_backend_report(db)}


class SourceIn(BaseModel):
    enabled: bool | None = None
    priority: int | None = None
    max_requests_per_run: int | None = None
    monthly_call_limit: int | None = None
    approved_hosts: list[str] | None = None
    sec_contact_email: str | None = None
    api_key: str | None = None
    reset_calls: bool = False


@router.get("/sources")
def get_sources(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    from backend.services.discovery_engine.sources import source_report

    return {"items": source_report(db)}


@router.patch("/sources/{name}")
def patch_source(
    name: str,
    body: SourceIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    from backend.services.discovery_engine.sources import source_report, update_source

    try:
        update_source(db, name, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": source_report(db)}


@router.get("/settings")
def get_settings(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    row = ensure_settings(db)
    db.commit()
    return _settings_out(row)


@router.patch("/settings")
def patch_settings(
    body: LimitsIn,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    del admin
    try:
        row = update_limits(
            db,
            enabled=body.enabled,
            max_concurrency=body.max_concurrency,
            max_runs_per_tick=body.max_runs_per_tick,
            max_runs_per_hour=body.max_runs_per_hour,
            max_runs_per_day=body.max_runs_per_day,
            store_name_only=body.store_name_only,
            serve_name_only=body.serve_name_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _settings_out(row)
