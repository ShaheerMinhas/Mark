"""Queue semantics: claiming, leases, retries and crash recovery (Phase 2)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from backend.models import QueuedJob, QueuedJobStatus
from backend.services.jobqueue import DbJobQueue

QUEUE = DbJobQueue()
JOB_TYPE = "test_job"


def test_enqueue_then_claim_round_trip(db):
    QUEUE.enqueue(db, JOB_TYPE, {"search_job_id": 7})

    handle = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE])

    assert handle is not None
    assert handle.job_type == JOB_TYPE
    assert handle.payload == {"search_job_id": 7}
    assert handle.attempts == 1
    assert handle.trace_id


def test_claimed_job_is_not_handed_out_twice(db):
    QUEUE.enqueue(db, JOB_TYPE, {})

    first = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE])
    second = QUEUE.claim(db, worker_id="w2", job_types=[JOB_TYPE])

    assert first is not None
    assert second is None


def test_claim_ignores_other_job_types(db):
    QUEUE.enqueue(db, "other_type", {})

    assert QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE]) is None


def test_claim_respects_scheduled_delay(db):
    QUEUE.enqueue(db, JOB_TYPE, {}, available_in=600)

    assert QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE]) is None


def test_discovery_is_claimed_ahead_of_an_older_enrichment(db):
    """A leftover email pass must not stall the search the user just started."""
    QUEUE.enqueue(db, "search_enrichment", {"n": "old"})
    QUEUE.enqueue(db, "search_discovery", {"n": "new"})

    handle = QUEUE.claim(
        db,
        worker_id="w1",
        job_types=["search_discovery", "search_enrichment"],
    )

    assert handle is not None
    assert handle.job_type == "search_discovery"
    assert handle.payload["n"] == "new"


def test_jobs_are_claimed_oldest_first(db):
    QUEUE.enqueue(db, JOB_TYPE, {"n": 1})
    QUEUE.enqueue(db, JOB_TYPE, {"n": 2})

    first = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE])
    QUEUE.complete(db, first.id)
    second = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE])

    assert first.payload["n"] == 1
    assert second.payload["n"] == 2


def test_complete_marks_the_job_done(db):
    job = QUEUE.enqueue(db, JOB_TYPE, {})
    handle = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE])

    QUEUE.complete(db, handle.id)

    db.refresh(job)
    assert job.status == QueuedJobStatus.DONE.value
    assert job.finished_at is not None


def test_failure_with_retry_requeues_the_job(db):
    job = QUEUE.enqueue(db, JOB_TYPE, {}, max_attempts=3)
    handle = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE])

    requeued = QUEUE.fail(db, handle.id, error="boom", retry_in=0)

    db.refresh(job)
    assert requeued is True
    assert job.status == QueuedJobStatus.QUEUED.value
    assert job.last_error == "boom"
    assert QUEUE.claim(db, worker_id="w2", job_types=[JOB_TYPE]) is not None


def test_failure_on_last_attempt_is_terminal(db):
    job = QUEUE.enqueue(db, JOB_TYPE, {}, max_attempts=1)
    handle = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE])
    assert handle.is_final_attempt

    requeued = QUEUE.fail(db, handle.id, error="boom", retry_in=0)

    db.refresh(job)
    assert requeued is False
    assert job.status == QueuedJobStatus.FAILED.value


def test_expired_lease_is_reclaimed_for_another_worker(db):
    """This is the crash recovery the daemon threads never had."""
    job = QUEUE.enqueue(db, JOB_TYPE, {}, max_attempts=3)
    QUEUE.claim(db, worker_id="dead-worker", job_types=[JOB_TYPE])
    job.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    reclaimed = QUEUE.reclaim_expired(db)

    assert reclaimed == 1
    handle = QUEUE.claim(db, worker_id="live-worker", job_types=[JOB_TYPE])
    assert handle is not None
    assert handle.attempts == 2


def test_expired_lease_out_of_attempts_fails(db):
    job = QUEUE.enqueue(db, JOB_TYPE, {}, max_attempts=1)
    QUEUE.claim(db, worker_id="dead-worker", job_types=[JOB_TYPE])
    job.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    QUEUE.reclaim_expired(db)

    db.refresh(job)
    assert job.status == QueuedJobStatus.FAILED.value
    assert job.last_error == "worker lease expired"


def test_heartbeat_extends_the_lease(db):
    job = QUEUE.enqueue(db, JOB_TYPE, {})
    handle = QUEUE.claim(db, worker_id="w1", job_types=[JOB_TYPE], lease_seconds=30)
    db.refresh(job)
    original = job.lease_expires_at

    assert QUEUE.heartbeat(db, handle.id, lease_seconds=600) is True

    db.refresh(job)
    assert job.lease_expires_at > original
    assert QUEUE.reclaim_expired(db) == 0


def test_worker_runs_the_registered_handler(db, monkeypatch):
    from backend import worker as worker_module

    seen = []

    def handler(session, handle):
        seen.append(handle.payload)

    monkeypatch.setitem(worker_module._HANDLERS, JOB_TYPE, handler)
    monkeypatch.setattr(worker_module, "_load_handlers", lambda: None)
    job = QUEUE.enqueue(db, JOB_TYPE, {"n": 42})

    did_work = worker_module.Worker(job_types=[JOB_TYPE]).poll_once()

    db.refresh(job)
    assert did_work is True
    assert seen == [{"n": 42}]
    assert job.status == QueuedJobStatus.DONE.value


def test_worker_records_handler_failure_without_losing_the_job(db, monkeypatch):
    from backend import worker as worker_module

    def handler(session, handle):
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(worker_module._HANDLERS, JOB_TYPE, handler)
    monkeypatch.setattr(worker_module, "_load_handlers", lambda: None)
    job = QUEUE.enqueue(db, JOB_TYPE, {}, max_attempts=2)

    worker_module.Worker(job_types=[JOB_TYPE]).poll_once()

    db.refresh(job)
    assert job.status == QueuedJobStatus.QUEUED.value
    assert "handler exploded" in job.last_error


def test_worker_reports_no_work_on_an_empty_queue(db, monkeypatch):
    from backend import worker as worker_module

    monkeypatch.setattr(worker_module, "_load_handlers", lambda: None)

    assert worker_module.Worker(job_types=[JOB_TYPE]).poll_once() is False
    assert db.query(QueuedJob).count() == 0


def test_a_long_job_is_not_stolen_while_the_worker_is_still_running(db, monkeypatch):
    """Enrichment outlives the default lease; heartbeat must keep the claim."""
    from backend import worker as worker_module

    started = threading.Event()
    finished = threading.Event()

    def handler(session, handle):
        started.set()
        time.sleep(3.2)
        finished.set()

    monkeypatch.setattr(worker_module, "WORKER_LEASE_SECONDS", 2)
    monkeypatch.setitem(worker_module._HANDLERS, JOB_TYPE, handler)
    monkeypatch.setattr(worker_module, "_load_handlers", lambda: None)
    job = QUEUE.enqueue(db, JOB_TYPE, {})

    thread = threading.Thread(target=lambda: worker_module.Worker(job_types=[JOB_TYPE]).poll_once())
    thread.start()
    assert started.wait(timeout=2)
    time.sleep(2.4)
    assert QUEUE.reclaim_expired(db) == 0
    thread.join(timeout=5)
    assert finished.is_set()
    db.refresh(job)
    assert job.status == QueuedJobStatus.DONE.value
