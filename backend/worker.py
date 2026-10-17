"""Queue worker.

Run as its own process::

    python -m backend.worker

or let the API process host it by leaving ``MARK_INLINE_WORKER`` enabled, which
keeps the existing single-service Railway deployment working. Either way the job
state lives in the database, so a restart resumes work instead of losing it.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid
from typing import Callable

from sqlalchemy.orm import Session

from backend.config import (
    WORKER_CONCURRENCY,
    WORKER_IDLE_SLEEP_SECONDS,
    WORKER_LEASE_SECONDS,
)
from backend.database import SessionLocal
from backend.models import JobType
from backend.observability import configure_logging, log_event
from backend.services.jobqueue import JobHandle, queue

logger = logging.getLogger(__name__)

#: Job type -> handler. Handlers receive a fresh session and the job payload.
JobHandler = Callable[[Session, JobHandle], None]
_HANDLERS: dict[str, JobHandler] = {}

#: Reclaim sweeps are cheap but pointless to run on every poll.
RECLAIM_INTERVAL_SECONDS = 30.0


def register(job_type: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(handler: JobHandler) -> JobHandler:
        _HANDLERS[job_type] = handler
        return handler

    return decorator


def _load_handlers() -> None:
    """Import modules that register handlers, avoiding circular imports at module load."""
    from backend.services import search_jobs  # noqa: F401


def handled_job_types() -> list[str]:
    _load_handlers()
    return sorted(_HANDLERS)


def _heartbeat_loop(handle_id: int, stop: threading.Event) -> None:
    """Renew the queue lease until the handler returns.

    Enrichment of a large lead pool routinely outlives the default lease. Without
    this, a second worker steals the same job and both leave leads stuck in
    ``processing``.
    """
    interval = max(1.0, min(15.0, WORKER_LEASE_SECONDS / 4))
    while not stop.wait(interval):
        db = SessionLocal()
        try:
            queue.heartbeat(db, handle_id, lease_seconds=WORKER_LEASE_SECONDS)
        except Exception:
            logger.exception("job %s heartbeat failed", handle_id)
        finally:
            db.close()


def run_job(handle: JobHandle) -> None:
    """Execute one claimed job and settle its queue row."""
    _load_handlers()
    handler = _HANDLERS.get(handle.job_type)
    db = SessionLocal()
    stop_hb = threading.Event()
    hb_thread: threading.Thread | None = None
    try:
        if handler is None:
            queue.fail(db, handle.id, error=f"no handler for {handle.job_type}")
            log_event(
                logger,
                "job.unhandled",
                level=logging.ERROR,
                job_id=handle.id,
                job_type=handle.job_type,
            )
            return
        started = time.monotonic()
        log_event(
            logger,
            "job.start",
            job_id=handle.id,
            job_type=handle.job_type,
            trace_id=handle.trace_id,
            attempt=handle.attempts,
        )
        hb_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(handle.id, stop_hb),
            name=f"mark-hb-{handle.id}",
            daemon=True,
        )
        hb_thread.start()
        try:
            handler(db, handle)
        except Exception as exc:
            db.rollback()
            retry_in = None if handle.is_final_attempt else 5.0 * handle.attempts
            requeued = queue.fail(db, handle.id, error=repr(exc), retry_in=retry_in)
            log_event(
                logger,
                "job.error",
                level=logging.ERROR,
                job_id=handle.id,
                job_type=handle.job_type,
                trace_id=handle.trace_id,
                attempt=handle.attempts,
                requeued=requeued,
                error=type(exc).__name__,
            )
            logger.exception("job %s failed", handle.id)
            return
        queue.complete(db, handle.id)
        log_event(
            logger,
            "job.done",
            job_id=handle.id,
            job_type=handle.job_type,
            trace_id=handle.trace_id,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        stop_hb.set()
        if hb_thread is not None:
            hb_thread.join(timeout=1.0)
        db.close()


class Worker:
    def __init__(self, *, worker_id: str | None = None, job_types: list[str] | None = None) -> None:
        self.worker_id = worker_id or f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.job_types = job_types or handled_job_types()
        self._stop = threading.Event()
        self._last_reclaim = 0.0

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def poll_once(self) -> bool:
        """Claim and run at most one job. Returns ``True`` if work was done."""
        db = SessionLocal()
        try:
            self._maybe_reclaim(db)
            handle = queue.claim(
                db,
                worker_id=self.worker_id,
                job_types=self.job_types,
                lease_seconds=WORKER_LEASE_SECONDS,
            )
        except Exception:
            logger.exception("worker %s could not claim a job", self.worker_id)
            return False
        finally:
            db.close()

        if handle is None:
            return False
        run_job(handle)
        return True

    def _maybe_reclaim(self, db: Session) -> None:
        now = time.monotonic()
        if now - self._last_reclaim < RECLAIM_INTERVAL_SECONDS:
            return
        self._last_reclaim = now
        reclaimed = queue.reclaim_expired(db)
        if reclaimed:
            log_event(logger, "queue.reclaimed", count=reclaimed, worker_id=self.worker_id)

    def run(self) -> None:
        log_event(
            logger,
            "worker.start",
            worker_id=self.worker_id,
            job_types=",".join(self.job_types),
        )
        while not self._stop.is_set():
            try:
                did_work = self.poll_once()
            except Exception:
                logger.exception("worker %s loop error", self.worker_id)
                did_work = False
            if not did_work:
                self._stop.wait(WORKER_IDLE_SLEEP_SECONDS)
        log_event(logger, "worker.stop", worker_id=self.worker_id)


_inline_workers: list[Worker] = []
_inline_threads: list[threading.Thread] = []


def start_inline_workers(count: int | None = None) -> list[Worker]:
    """Host workers inside the API process.

    Unlike the daemon threads this replaces, losing these threads does not lose
    the work: the queue row stays claimed only until its lease expires, after
    which any worker picks it up again.
    """
    if _inline_workers:
        return _inline_workers
    for _ in range(max(1, count or WORKER_CONCURRENCY)):
        worker = Worker()
        thread = threading.Thread(
            target=worker.run, name=f"mark-worker-{worker.worker_id}", daemon=True
        )
        thread.start()
        _inline_workers.append(worker)
        _inline_threads.append(thread)
    return _inline_workers


def stop_inline_workers(timeout: float = 5.0) -> None:
    for worker in _inline_workers:
        worker.stop()
    for thread in _inline_threads:
        thread.join(timeout=timeout)
    _inline_workers.clear()
    _inline_threads.clear()


def main() -> None:
    configure_logging(os.getenv("MARK_LOG_LEVEL"))
    from backend.database import init_db

    init_db()

    workers = [Worker() for _ in range(max(1, WORKER_CONCURRENCY))]
    threads = [
        threading.Thread(target=w.run, name=f"mark-worker-{w.worker_id}") for w in workers
    ]

    def shutdown(*_: object) -> None:
        for worker in workers:
            worker.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
