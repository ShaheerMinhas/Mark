"""Phase 12 — transient search-engine failures are retried, not abandoned.

On live runs more than half of the queries came back rate-limited; giving up on
the first error discarded most of the candidate pool.
"""

from __future__ import annotations

import threading

import pytest

from backend.models import SearchJob, SearchJobStatus, SearchQueryRun
from backend.services.ddg import SearchOutcome, SearchResponse
from backend.services.discovery import runner
from tests.conftest import serp_hit


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Keep the retry path instant; the delay itself is asserted separately."""
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("backend.services.search_jobs.enqueue_enrichment", lambda *a, **k: None)


@pytest.fixture
def job(db, user):
    row = SearchJob(
        name="retry",
        prompt="Marketing managers in Sweden",
        filters={"job_titles": ["Marketing Manager"], "active_only": False},
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
        trace_id="retrytrace000000",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def hits():
    return [serp_hit("anna-lind", "Anna Lind", "Marketing Manager", "Nordic AB")]


def test_a_rate_limited_query_is_retried_and_can_succeed(db, job, monkeypatch):
    calls: dict[str, int] = {}

    def flaky(query, **kwargs):
        calls[query] = calls.get(query, 0) + 1
        if calls[query] == 1:
            return SearchResponse(SearchOutcome.RATE_LIMITED, detail="429")
        return SearchResponse(SearchOutcome.OK, hits=hits())

    monkeypatch.setattr(runner, "search_text", flaky)
    runner.run_discovery(db, job)

    db.refresh(job)
    assert all(count >= 2 for count in calls.values())
    assert job.failed_queries == 0
    assert job.candidates_found > 0


def test_a_timeout_is_retried(db, job, monkeypatch):
    """Each query times out once, then succeeds on its retry."""
    calls: dict[str, int] = {}

    def flaky(query, **kwargs):
        calls[query] = calls.get(query, 0) + 1
        if calls[query] == 1:
            return SearchResponse(SearchOutcome.TIMEOUT, detail="timed out")
        return SearchResponse(SearchOutcome.OK, hits=hits())

    monkeypatch.setattr(runner, "search_text", flaky)
    runner.run_discovery(db, job)

    db.refresh(job)
    assert job.candidates_found > 0
    assert job.failed_queries == 0
    assert all(count == 2 for count in calls.values())


def test_retries_are_bounded(db, job, monkeypatch):
    calls: dict[str, int] = {}

    def always_limited(query, **kwargs):
        calls[query] = calls.get(query, 0) + 1
        return SearchResponse(SearchOutcome.RATE_LIMITED, detail="429")

    monkeypatch.setattr(runner, "search_text", always_limited)
    monkeypatch.setattr(runner, "RATE_LIMIT_STREAK_LIMIT", 10_000)
    runner.run_discovery(db, job)

    assert calls, "no query was attempted"
    assert max(calls.values()) == runner.MAX_QUERY_ATTEMPTS


def test_a_permanent_error_is_not_retried(db, job, monkeypatch):
    calls: dict[str, int] = {}

    def broken(query, **kwargs):
        calls[query] = calls.get(query, 0) + 1
        return SearchResponse(SearchOutcome.ERROR, detail="bad request")

    monkeypatch.setattr(runner, "search_text", broken)
    monkeypatch.setattr(runner, "RATE_LIMIT_STREAK_LIMIT", 10_000)
    runner.run_discovery(db, job)

    assert max(calls.values()) == 1


def test_failed_queries_counted_once_per_query_not_per_attempt(db, job, monkeypatch):
    monkeypatch.setattr(
        runner,
        "search_text",
        lambda q, **kw: SearchResponse(SearchOutcome.RATE_LIMITED, detail="429"),
    )
    monkeypatch.setattr(runner, "RATE_LIMIT_STREAK_LIMIT", 10_000)
    runner.run_discovery(db, job)

    db.refresh(job)
    runs = db.query(SearchQueryRun).filter(SearchQueryRun.search_job_id == job.id).all()
    abandoned = [r for r in runs if r.status == "failed"]
    assert job.failed_queries == len(abandoned)
    # Each abandoned query really was tried the full number of times.
    assert all(r.attempts == runner.MAX_QUERY_ATTEMPTS for r in abandoned)


def test_the_backoff_grows_between_attempts(db, job, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(
        runner,
        "search_text",
        lambda q, **kw: SearchResponse(SearchOutcome.RATE_LIMITED, detail="429"),
    )
    monkeypatch.setattr(runner, "RATE_LIMIT_STREAK_LIMIT", 10_000)
    runner.run_discovery(db, job)

    assert slept, "no backoff was applied"
    assert min(slept) == runner._RETRY_BACKOFF_SECONDS[0]
    assert max(slept) == runner._RETRY_BACKOFF_SECONDS[-1]


def test_every_query_failing_does_not_report_success(db, job, monkeypatch):
    monkeypatch.setattr(
        runner,
        "search_text",
        lambda q, **kw: SearchResponse(SearchOutcome.ERROR, detail="down"),
    )
    runner.run_discovery(db, job)

    db.refresh(job)
    assert job.status == SearchJobStatus.FAILED
    assert job.candidates_found == 0


def test_a_partial_failure_still_returns_leads(db, job, monkeypatch):
    state = {"n": 0}
    lock = threading.Lock()

    def half_broken(query, **kwargs):
        with lock:
            state["n"] += 1
            n = state["n"]
        if n % 2 == 0:
            return SearchResponse(SearchOutcome.ERROR, detail="down")
        return SearchResponse(SearchOutcome.OK, hits=hits())

    monkeypatch.setattr(runner, "search_text", half_broken)
    runner.run_discovery(db, job)

    db.refresh(job)
    assert job.status == SearchJobStatus.PARTIALLY_COMPLETED
    assert job.candidates_found > 0
    assert job.failed_queries > 0
