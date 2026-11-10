"""Platform-operator controls for global discovery. No customer can call these."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import require_platform_admin
from backend.models import DiscoveryRun, DiscoveryTarget, User
from backend.services.discovery_engine.schedule import next_run_at, validate_timezone
from backend.services.discovery_engine.settings import ensure_settings
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


@router.get("/settings")
def get_settings(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    del admin
    row = ensure_settings(db)
    db.commit()
    return {
        "enabled": bool(row.enabled),
        "live_public_search_enabled": False,
        "paid_providers_enabled": False,
        "max_concurrency": row.max_concurrency,
        "max_runs_per_tick": row.max_runs_per_tick,
    }
