"""Worker heartbeat stored in the database. It is not the planner lease."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.models import WorkerHeartbeat

STALE_AFTER_SECONDS = 120


def beat(db: Session, worker_id: str, *, kind: str = "discovery", status: str = "running") -> WorkerHeartbeat:
    row = db.get(WorkerHeartbeat, worker_id)
    now = datetime.utcnow()
    if row is None:
        row = WorkerHeartbeat(worker_id=worker_id, kind=kind, status=status, beat_at=now)
        db.add(row)
    else:
        row.kind = kind
        row.status = status
        row.beat_at = now
    db.commit()
    db.refresh(row)
    return row


def worker_health(db: Session, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    rows = db.query(WorkerHeartbeat).all()
    if not rows:
        return {"status": "unknown", "detail": "No worker has written a heartbeat."}
    latest = max(rows, key=lambda row: row.beat_at or datetime.min)
    age = (now - latest.beat_at).total_seconds() if latest.beat_at else None
    if latest.status == "stopping":
        state = "stopping"
    elif age is not None and age <= STALE_AFTER_SECONDS:
        state = "running"
    else:
        state = "stale"
    return {
        "status": state,
        "worker_id": latest.worker_id,
        "kind": latest.kind,
        "beat_at": latest.beat_at,
        "age_seconds": None if age is None else int(age),
    }
