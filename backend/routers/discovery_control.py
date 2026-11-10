"""Platform-operator controls for global discovery. No customer can call these."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import require_platform_admin
from backend.models import DiscoveryRun, DiscoveryTarget, User
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


class SuppressionIn(BaseModel):
    kind: str
    value: str
    reason: str | None = None


def _check_target(body: TargetIn) -> None:
    if body.schedule_type not in {"interval", "daily", "weekdays"}:
        raise HTTPException(status_code=400, detail="schedule_type must be interval, daily, or weekdays")
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
    row = DiscoveryTarget(
        name=body.name.strip(),
        priority=body.priority,
        scheduling_status="paused",
        schedule_type=body.schedule_type,
        timezone=body.timezone,
        interval_minutes=body.interval_minutes,
        hour=body.hour,
        minute=body.minute,
        max_people_per_run=body.max_people_per_run,
        max_queries_per_run=body.max_queries_per_run,
        refresh_hours=body.refresh_hours,
        countries=body.countries,
        industries=body.industries,
        titles=body.titles,
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
        "paid_providers_enabled": False,
        "max_concurrency": row.max_concurrency,
        "max_runs_per_tick": row.max_runs_per_tick,
        "max_runs_per_hour": row.max_runs_per_hour,
        "max_runs_per_day": row.max_runs_per_day,
        "detail": "Live search and paid providers stay off in this build, even when scheduling is enabled.",
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
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _settings_out(row)
