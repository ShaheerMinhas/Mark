"""Durable job queue backed by the application database.

Chosen over Redis + RQ because the project already runs Postgres on Railway and
has no Redis, and because job state has to be persistent and queryable anyway so
the API can answer progress polls. A lease plus heartbeat gives us the crash
recovery the previous daemon threads never had: if a worker dies mid-job, the
lease expires and the job is reclaimed instead of being silently lost.

``claim`` uses ``FOR UPDATE SKIP LOCKED`` on Postgres so several workers can pull
from the queue concurrently. SQLite falls back to a compare-and-set update, which
is sufficient because local development runs a single worker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, Sequence

from sqlalchemy import bindparam, case, text
from sqlalchemy.orm import Session

from backend.models import QueuedJob, QueuedJobStatus

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class JobHandle:
    """Snapshot of a claimed job, detached from the claiming session."""

    id: int
    job_type: str
    payload: dict
    attempts: int
    max_attempts: int
    trace_id: str

    @property
    def is_final_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


class JobQueue(Protocol):
    def enqueue(
        self,
        db: Session,
        job_type: str,
        payload: dict,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        available_in: float = 0.0,
        trace_id: str | None = None,
    ) -> QueuedJob: ...

    def claim(
        self,
        db: Session,
        *,
        worker_id: str,
        job_types: Sequence[str],
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> JobHandle | None: ...

    def heartbeat(self, db: Session, job_id: int, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool: ...

    def complete(self, db: Session, job_id: int) -> None: ...

    def fail(self, db: Session, job_id: int, *, error: str, retry_in: float | None = None) -> bool: ...

    def reclaim_expired(self, db: Session) -> int: ...


class DbJobQueue:
    """Database-backed :class:`JobQueue`."""

    def enqueue(
        self,
        db: Session,
        job_type: str,
        payload: dict,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        available_in: float = 0.0,
        trace_id: str | None = None,
    ) -> QueuedJob:
        job = QueuedJob(
            job_type=job_type,
            payload=payload or {},
            status=QueuedJobStatus.QUEUED.value,
            max_attempts=max_attempts,
            available_at=datetime.utcnow() + timedelta(seconds=max(0.0, available_in)),
            trace_id=trace_id or uuid.uuid4().hex[:16],
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def claim(
        self,
        db: Session,
        *,
        worker_id: str,
        job_types: Sequence[str],
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> JobHandle | None:
        if not job_types:
            return None
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            job_id = self._claim_postgres(db, worker_id, job_types, lease_seconds)
        else:
            job_id = self._claim_generic(db, worker_id, job_types, lease_seconds)
        if job_id is None:
            return None
        job = db.get(QueuedJob, job_id)
        if job is None:
            return None
        return JobHandle(
            id=job.id,
            job_type=job.job_type,
            payload=dict(job.payload or {}),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            trace_id=job.trace_id or "",
        )

    def _claim_postgres(
        self, db: Session, worker_id: str, job_types: Sequence[str], lease_seconds: int
    ) -> int | None:
        now = datetime.utcnow()
        # The order of ``job_types`` is the claim priority, so discovery can
        # jump a leftover enrichment instead of waiting behind it.
        priority = " ".join(
            f"WHEN inner_q.job_type = :jt_{index} THEN {index}"
            for index in range(len(job_types))
        )
        statement = text(
            f"""
            UPDATE job_queue AS q
               SET status = :running,
                   worker_id = :worker_id,
                   attempts = q.attempts + 1,
                   lease_expires_at = :lease_expires_at,
                   updated_at = :now
             WHERE q.id = (
                   SELECT inner_q.id
                     FROM job_queue AS inner_q
                    WHERE inner_q.status = :queued
                      AND inner_q.available_at <= :now
                      AND inner_q.job_type IN :job_types
                    ORDER BY CASE {priority} ELSE {len(job_types)} END,
                             inner_q.available_at, inner_q.id
                    LIMIT 1
                      FOR UPDATE SKIP LOCKED
             )
            RETURNING q.id
            """
        ).bindparams(bindparam("job_types", expanding=True))
        params = {
            "running": QueuedJobStatus.RUNNING.value,
            "queued": QueuedJobStatus.QUEUED.value,
            "worker_id": worker_id,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "now": now,
            "job_types": list(job_types),
        }
        for index, name in enumerate(job_types):
            params[f"jt_{index}"] = name
        result = db.execute(statement, params)
        row = result.first()
        db.commit()
        return int(row[0]) if row else None

    def _claim_generic(
        self, db: Session, worker_id: str, job_types: Sequence[str], lease_seconds: int
    ) -> int | None:
        """Compare-and-set claim for backends without SKIP LOCKED."""
        now = datetime.utcnow()
        for _ in range(5):
            type_priority = case(
                {name: index for index, name in enumerate(job_types)},
                value=QueuedJob.job_type,
                else_=len(job_types),
            )
            candidate = (
                db.query(QueuedJob)
                .filter(
                    QueuedJob.status == QueuedJobStatus.QUEUED.value,
                    QueuedJob.available_at <= now,
                    QueuedJob.job_type.in_(list(job_types)),
                )
                .order_by(type_priority, QueuedJob.available_at, QueuedJob.id)
                .first()
            )
            if candidate is None:
                return None
            updated = (
                db.query(QueuedJob)
                .filter(
                    QueuedJob.id == candidate.id,
                    QueuedJob.status == QueuedJobStatus.QUEUED.value,
                )
                .update(
                    {
                        QueuedJob.status: QueuedJobStatus.RUNNING.value,
                        QueuedJob.worker_id: worker_id,
                        QueuedJob.attempts: QueuedJob.attempts + 1,
                        QueuedJob.lease_expires_at: now + timedelta(seconds=lease_seconds),
                        QueuedJob.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated:
                return candidate.id
        return None

    def heartbeat(
        self, db: Session, job_id: int, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> bool:
        now = datetime.utcnow()
        updated = (
            db.query(QueuedJob)
            .filter(QueuedJob.id == job_id, QueuedJob.status == QueuedJobStatus.RUNNING.value)
            .update(
                {
                    QueuedJob.lease_expires_at: now + timedelta(seconds=lease_seconds),
                    QueuedJob.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(updated)

    def complete(self, db: Session, job_id: int) -> None:
        now = datetime.utcnow()
        db.query(QueuedJob).filter(QueuedJob.id == job_id).update(
            {
                QueuedJob.status: QueuedJobStatus.DONE.value,
                QueuedJob.lease_expires_at: None,
                QueuedJob.finished_at: now,
                QueuedJob.updated_at: now,
            },
            synchronize_session=False,
        )
        db.commit()

    def fail(
        self, db: Session, job_id: int, *, error: str, retry_in: float | None = None
    ) -> bool:
        """Mark a job failed, or requeue it when attempts remain.

        Returns ``True`` when the job was requeued for another attempt.
        """
        job = db.get(QueuedJob, job_id)
        if job is None:
            return False
        now = datetime.utcnow()
        job.updated_at = now
        job.last_error = (error or "")[:2000]
        if retry_in is not None and job.attempts < job.max_attempts:
            job.status = QueuedJobStatus.QUEUED.value
            job.available_at = now + timedelta(seconds=max(0.0, retry_in))
            job.lease_expires_at = None
            job.worker_id = None
            db.commit()
            return True
        job.status = QueuedJobStatus.FAILED.value
        job.lease_expires_at = None
        job.finished_at = now
        db.commit()
        return False

    def reclaim_expired(self, db: Session) -> int:
        """Requeue jobs whose worker died, failing those out of attempts."""
        now = datetime.utcnow()
        stale = (
            db.query(QueuedJob)
            .filter(
                QueuedJob.status == QueuedJobStatus.RUNNING.value,
                QueuedJob.lease_expires_at.isnot(None),
                QueuedJob.lease_expires_at < now,
            )
            .all()
        )
        for job in stale:
            job.updated_at = now
            job.lease_expires_at = None
            job.worker_id = None
            if job.attempts < job.max_attempts:
                job.status = QueuedJobStatus.QUEUED.value
                job.available_at = now
            else:
                job.status = QueuedJobStatus.FAILED.value
                job.finished_at = now
                job.last_error = "worker lease expired"
        if stale:
            db.commit()
        return len(stale)


#: Process-wide queue. Swapping this for an RQ-backed implementation later only
#: requires satisfying the :class:`JobQueue` protocol.
queue: JobQueue = DbJobQueue()
