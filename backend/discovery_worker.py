"""Discovery worker process.

Run as its own process::

    python -m backend.discovery_worker

The API process does not start this loop. Customer searches keep using
``python -m backend.worker``.
"""

from __future__ import annotations

import os
import random
import signal
import threading
import time
import uuid

from backend.config import LOG_LEVEL, WORKER_IDLE_SLEEP_SECONDS, WORKER_LEASE_SECONDS
from backend.database import SessionLocal, init_db
from backend.models import DiscoveryRun, JobType
from backend.observability import configure_logging, log_event
from backend.services.discovery_engine.backoff import RateLimited, backoff_seconds
from backend.services.discovery_engine.pipeline import execute_queued_job
from backend.services.discovery_engine.schedule import plan_due_targets, running_pipeline_count
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.jobqueue import queue

logger_name = "mark.discovery_worker"


def claim_one(db, worker_id: str):
    settings = ensure_settings(db)
    if running_pipeline_count(db) >= max(1, int(settings.max_concurrency or 1)):
        return None
    return queue.claim(
        db,
        worker_id=worker_id,
        job_types=[JobType.DISCOVERY_PIPELINE.value],
        lease_seconds=WORKER_LEASE_SECONDS,
    )


def handle_claimed(db, handle, *, jitter: float | None = None, heartbeat: bool = True) -> None:
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(max(1.0, WORKER_LEASE_SECONDS / 4)):
            beat = SessionLocal()
            try:
                queue.heartbeat(beat, handle.id, lease_seconds=WORKER_LEASE_SECONDS)
            finally:
                beat.close()

    thread = None
    if heartbeat:
        thread = threading.Thread(target=_beat, name=f"discovery-hb-{handle.id}", daemon=True)
        thread.start()
    try:
        execute_queued_job(db, handle.payload, attempt=handle.attempts)
        queue.complete(db, handle.id)
    except RateLimited as exc:
        delay = None
        if not handle.is_final_attempt:
            delay = backoff_seconds(handle.attempts, jitter=random.random() if jitter is None else jitter)
        requeued = queue.fail(db, handle.id, error=str(exc), retry_in=delay)
        run = db.get(DiscoveryRun, int((handle.payload or {}).get("run_id") or 0))
        if run is not None:
            run.status = "queued" if requeued else "failed"
            run.error = str(exc)
            if not requeued:
                run.finished_at = __import__("datetime").datetime.utcnow()
            db.commit()
    except Exception as exc:
        delay = None if handle.is_final_attempt else backoff_seconds(
            handle.attempts, jitter=random.random() if jitter is None else jitter
        )
        requeued = queue.fail(db, handle.id, error=repr(exc), retry_in=delay)
        run = db.get(DiscoveryRun, int((handle.payload or {}).get("run_id") or 0))
        if run is not None and not requeued:
            run.status = "failed"
            run.error = repr(exc)[:2000]
            run.finished_at = __import__("datetime").datetime.utcnow()
            db.commit()
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1)


def poll_once(worker_id: str) -> bool:
    db = SessionLocal()
    try:
        from backend.services.discovery_engine.heartbeat import beat

        beat(db, worker_id, kind="discovery", status="running")
        queue.reclaim_expired(db)
        plan_due_targets(db, planner_id=worker_id)
        handle = claim_one(db, worker_id)
        if handle is None:
            return False
        handle_claimed(db, handle)
        return True
    finally:
        db.close()


def main() -> None:
    configure_logging(os.getenv("MARK_LOG_LEVEL", LOG_LEVEL))
    init_db()
    worker_id = uuid.uuid4().hex[:12]
    stop = threading.Event()

    def shutdown(*_: object) -> None:
        stop.set()
        closing = SessionLocal()
        try:
            from backend.services.discovery_engine.heartbeat import beat

            beat(closing, worker_id, kind="discovery", status="stopping")
        except Exception:
            closing.rollback()
        finally:
            closing.close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log_event(__import__("logging").getLogger(logger_name), "discovery_worker.start", worker_id=worker_id)
    while not stop.is_set():
        did = False
        try:
            did = poll_once(worker_id)
        except Exception:
            __import__("logging").getLogger(logger_name).exception("discovery worker loop error")
        if not did:
            stop.wait(WORKER_IDLE_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
