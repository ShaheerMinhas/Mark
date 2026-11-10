"""Plan due targets into durable runs. Planning is locked. Claiming is separate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.models import (
    DiscoveryRun,
    DiscoveryTarget,
    JobType,
    QueuedJobStatus,
    SchedulerLock,
)
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.jobqueue import queue

SCHEDULER_LOCK = "discovery_planner"
MIN_INTERVAL_MINUTES = 15


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone {name}") from exc
    return name


def next_run_at(target: DiscoveryTarget, after_utc: datetime) -> datetime:
    """Next slot as naive UTC. Daily and weekday times are interpreted in the target timezone."""
    if target.schedule_type == "interval":
        minutes = max(MIN_INTERVAL_MINUTES, int(target.interval_minutes or 60))
        return after_utc + timedelta(minutes=minutes)
    tz = ZoneInfo(target.timezone or "UTC")
    local = after_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    candidate = local.replace(hour=int(target.hour or 0), minute=int(target.minute or 0), second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    if target.schedule_type == "weekdays":
        while candidate.weekday() >= 5:
            candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def try_acquire_planner(db: Session, owner: str, *, lease_seconds: int = 30) -> bool:
    """Compare-and-set lease. SQLite does not provide SKIP LOCKED; Postgres queue claims do."""
    now = datetime.utcnow()
    until = now + timedelta(seconds=lease_seconds)
    row = db.get(SchedulerLock, SCHEDULER_LOCK)
    if row is None:
        try:
            db.add(SchedulerLock(name=SCHEDULER_LOCK, owner=owner, lease_expires_at=until))
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False
    if row.lease_expires_at and row.lease_expires_at > now and row.owner != owner:
        return False
    updated = (
        db.query(SchedulerLock)
        .filter(
            SchedulerLock.name == SCHEDULER_LOCK,
            (SchedulerLock.lease_expires_at.is_(None))
            | (SchedulerLock.lease_expires_at <= now)
            | (SchedulerLock.owner == owner),
        )
        .update({SchedulerLock.owner: owner, SchedulerLock.lease_expires_at: until}, synchronize_session=False)
    )
    db.commit()
    return updated == 1


def within_run_caps(db: Session, now: datetime) -> bool:
    settings = ensure_settings(db)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    hourly = db.query(DiscoveryRun).filter(DiscoveryRun.created_at >= hour_ago).count()
    daily = db.query(DiscoveryRun).filter(DiscoveryRun.created_at >= day_ago).count()
    return hourly < int(settings.max_runs_per_hour or 4) and daily < int(settings.max_runs_per_day or 24)


def enqueue_run(db: Session, target: DiscoveryTarget, *, slot: datetime, key: str) -> DiscoveryRun | None:
    if db.query(DiscoveryRun).filter(DiscoveryRun.idempotency_key == key).first():
        return None
    run = DiscoveryRun(
        target_id=target.id,
        idempotency_key=key,
        status="queued",
        people_limit=int(target.max_people_per_run or 25),
        query_limit=int(target.max_queries_per_run or 5),
        scheduled_for=slot,
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None
    queued = queue.enqueue(
        db,
        JobType.DISCOVERY_PIPELINE.value,
        {"run_id": run.id},
        max_attempts=3,
    )
    run.queue_job_id = queued.id
    db.commit()
    db.refresh(run)
    return run


def enqueue_run_now(db: Session, target: DiscoveryTarget) -> DiscoveryRun:
    now = datetime.utcnow()
    if not within_run_caps(db, now):
        raise RuntimeError("Hourly or daily run cap has been reached")
    key = f"run-now:{target.id}:{now.isoformat()}"
    run = enqueue_run(db, target, slot=now, key=key)
    if run is None:
        raise RuntimeError("Could not queue that run")
    return run


def plan_due_targets(db: Session, *, now: datetime | None = None, planner_id: str) -> int:
    """Create runs for due active targets. Returns how many new runs were queued.

    Disabled settings, paused targets, and stopped targets create nothing.
    Existing queued runs are left in place.
    """
    if not try_acquire_planner(db, planner_id):
        return 0
    now = now or datetime.utcnow()
    settings = ensure_settings(db)
    if not settings.enabled:
        db.commit()
        return 0
    limit = max(1, int(settings.max_runs_per_tick or 1))
    due = (
        db.query(DiscoveryTarget)
        .filter(
            DiscoveryTarget.scheduling_status == "active",
            DiscoveryTarget.next_run_at.isnot(None),
            DiscoveryTarget.next_run_at <= now,
        )
        .order_by(DiscoveryTarget.priority.asc(), DiscoveryTarget.id.asc())
        .limit(limit)
        .all()
    )
    created = 0
    for target in due:
        if not within_run_caps(db, now):
            break
        slot = target.next_run_at or now
        key = f"target:{target.id}:slot:{slot.isoformat()}"
        if db.query(DiscoveryRun).filter(DiscoveryRun.idempotency_key == key).first():
            target.next_run_at = next_run_at(target, slot)
            target.updated_at = now
            db.commit()
            continue
        before = db.query(DiscoveryRun).count()
        run = enqueue_run(db, target, slot=slot, key=key)
        if run is None:
            continue
        target = db.get(DiscoveryTarget, target.id)
        target.next_run_at = next_run_at(target, slot)
        target.updated_at = now
        db.commit()
        created += db.query(DiscoveryRun).count() - before
    return created


def running_pipeline_count(db: Session) -> int:
    from backend.models import QueuedJob

    return (
        db.query(QueuedJob)
        .filter(
            QueuedJob.job_type == JobType.DISCOVERY_PIPELINE.value,
            QueuedJob.status == QueuedJobStatus.RUNNING.value,
        )
        .count()
    )
