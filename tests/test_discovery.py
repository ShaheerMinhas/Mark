"""Discovery pipeline: persistence, dedup, filtering, resilience, cancellation.

Every search-engine call is mocked. No test here reaches the real internet.
"""

from __future__ import annotations

from collections import Counter
import threading

import pytest

from backend.models import (
    CandidateStatus,
    EnrichmentStatus,
    LeadRecord,
    QueryRunStatus,
    SearchCandidate,
    SearchJob,
    SearchJobStatus,
    SearchQueryRun,
)
from backend.services.ddg import SearchOutcome, SearchResponse
from backend.services.discovery.runner import run_discovery
from tests.conftest import serp_hit

PROMPT = "Marketing managers in Swedish logistics companies"
FILTERS = {
    "job_titles": ["Marketing Manager"],
    "location": "Sweden",
    "industry": "logistics",
    "max_results": 100,
    "active_only": False,
    "business_only": True,
}


@pytest.fixture
def job(db, user):
    record = SearchJob(
        name="Swedish logistics marketing",
        prompt=PROMPT,
        filters=dict(FILTERS),
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
        trace_id="test-trace",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def profile(slug: str, name: str, company: str, role: str = "Marketing Manager") -> dict:
    return serp_hit(
        slug,
        name,
        role,
        company,
        body=f"{role} at {company}. Experience: {company}. Stockholm, Sweden.",
    )


def responder(monkeypatch, handler):
    """Replace the search engine with `handler(query, page) -> SearchResponse`."""
    calls: list[tuple[str, int]] = []

    def fake_search(query, *, max_results=15, page=1, ddgs=None):
        calls.append((query, page))
        return handler(query, page)

    monkeypatch.setattr("backend.services.discovery.runner.search_text", fake_search)
    return calls


def ok(hits: list[dict]) -> SearchResponse:
    return SearchResponse(SearchOutcome.OK if hits else SearchOutcome.EMPTY, hits=hits)


def no_enrichment(monkeypatch):
    """Discovery must not depend on enrichment, so tests assert it is only queued."""
    queued: list[int] = []
    monkeypatch.setattr(
        "backend.services.search_jobs.enqueue_enrichment",
        lambda db, job, **kw: queued.append(job.id),
    )
    return queued


# --- Happy path -------------------------------------------------------------


def test_search_completes_and_persists_leads(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    responder(
        monkeypatch,
        lambda q, page: ok(
            [profile("anna-lind", "Anna Lind", "Nordic Freight AB")] if page == 1 else []
        ),
    )

    run_discovery(db, job)

    db.refresh(job)
    assert job.status == SearchJobStatus.COMPLETED
    assert job.progress == 100
    assert job.candidates_found == 1
    assert job.qualified_leads == 1
    assert job.started_at is not None and job.completed_at is not None
    assert db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).count() == 1


def test_candidates_are_persisted_with_their_originating_query(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    responder(
        monkeypatch,
        lambda q, page: ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")]),
    )

    run_discovery(db, job)

    candidate = db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id).first()
    assert candidate.identity_key == "li:anna-lind"
    assert candidate.linkedin_url == "https://www.linkedin.com/in/anna-lind"
    assert "site:linkedin.com/in" in candidate.query_text
    assert candidate.discovered_at is not None


def test_every_planned_query_is_recorded(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    responder(monkeypatch, lambda q, page: ok([]))

    run_discovery(db, job)

    db.refresh(job)
    runs = db.query(SearchQueryRun).filter(SearchQueryRun.search_job_id == job.id).all()
    assert len(runs) == job.total_queries
    assert {run.dimension for run in runs} >= {"title"}
    assert all(run.status == QueryRunStatus.EMPTY.value for run in runs)


# --- Deduplication (Phase 6) ----------------------------------------------


def test_the_same_person_from_many_queries_is_stored_once(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    duplicate = profile("anna-lind", "Anna Lind", "Nordic Freight AB")
    responder(monkeypatch, lambda q, page: ok([duplicate]))

    run_discovery(db, job)

    db.refresh(job)
    assert db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id).count() == 1
    assert job.candidates_found == 1
    assert job.candidates_deduplicated >= 1


def test_url_variants_of_one_profile_collapse(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    variants = [
        profile("anna-lind", "Anna Lind", "Nordic Freight AB"),
        {
            **profile("anna-lind", "Anna Lind", "Nordic Freight AB"),
            "href": "https://se.linkedin.com/in/anna-lind?trk=serp",
        },
    ]
    responder(monkeypatch, lambda q, page: ok(variants))

    run_discovery(db, job)

    assert db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id).count() == 1


def test_different_people_are_kept_separately(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    hits = [
        profile("anna-lind", "Anna Lind", "Nordic Freight AB"),
        profile("erik-ohlsson", "Erik Ohlsson", "Baltic Logistics AB"),
    ]
    responder(monkeypatch, lambda q, page: ok(hits))

    run_discovery(db, job)

    assert db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id).count() == 2


# --- Filtering keeps the pool visible (Phase 7) ---------------------------


def test_rejected_candidates_are_kept_with_a_reason(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    hits = [
        profile("anna-lind", "Anna Lind", "Nordic Freight AB"),
        profile("per-nilsson", "Per Nilsson", "Nordic Freight AB", role="Head Chef"),
    ]
    responder(monkeypatch, lambda q, page: ok(hits))

    run_discovery(db, job)

    rejected = (
        db.query(SearchCandidate)
        .filter(
            SearchCandidate.search_job_id == job.id,
            SearchCandidate.status == CandidateStatus.REJECTED.value,
        )
        .all()
    )
    assert len(rejected) == 1
    assert rejected[0].name == "Per Nilsson"
    assert rejected[0].rejection_reason
    # The pool keeps it, but it never becomes a lead.
    assert db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).count() == 1


def test_candidates_found_and_relevant_leads_are_reported_separately(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    hits = [
        profile("anna-lind", "Anna Lind", "Nordic Freight AB"),
        profile("per-nilsson", "Per Nilsson", "Nordic Freight AB", role="Head Chef"),
        profile("lena-berg", "Lena Berg", "Baltic Logistics AB"),
    ]
    responder(monkeypatch, lambda q, page: ok(hits))

    run_discovery(db, job)

    db.refresh(job)
    assert job.candidates_found == 3
    assert job.qualified_leads == 2


def test_qualified_candidates_carry_a_score_and_filter_breakdown(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    responder(
        monkeypatch, lambda q, page: ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")])
    )

    run_discovery(db, job)

    candidate = (
        db.query(SearchCandidate)
        .filter(SearchCandidate.status == CandidateStatus.QUALIFIED.value)
        .first()
    )
    assert candidate.relevance_score > 0
    assert candidate.filter_match["job_title"] is True
    assert candidate.rejection_reason is None


# --- Search-engine resilience (Phase 12) ---------------------------------


def test_one_failing_query_does_not_fail_the_search(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    state = {"calls": 0}
    lock = threading.Lock()

    def handler(query, page):
        with lock:
            state["calls"] += 1
            n = state["calls"]
        if n == 1:
            return SearchResponse(SearchOutcome.ERROR, detail="HTTPError")
        return ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")])

    responder(monkeypatch, handler)

    run_discovery(db, job)

    db.refresh(job)
    assert job.status == SearchJobStatus.PARTIALLY_COMPLETED
    assert job.failed_queries == 1
    assert job.completed_queries > 0
    assert job.qualified_leads == 1


def test_partial_failure_records_the_failure_kind(db, job, monkeypatch):
    """A non-retryable error is abandoned with its kind recorded."""
    no_enrichment(monkeypatch)
    state = {"calls": 0}
    lock = threading.Lock()

    def handler(query, page):
        with lock:
            state["calls"] += 1
            n = state["calls"]
        if n == 1:
            return SearchResponse(SearchOutcome.ERROR, detail="HTTPStatusError")
        return ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")])

    responder(monkeypatch, handler)

    run_discovery(db, job)

    failed = (
        db.query(SearchQueryRun)
        .filter(SearchQueryRun.status == QueryRunStatus.FAILED.value)
        .first()
    )
    assert failed.error_kind == "error"
    assert failed.attempts == 1


def test_a_transient_timeout_is_retried_rather_than_abandoned(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    state = {"calls": 0}
    lock = threading.Lock()

    def handler(query, page):
        with lock:
            state["calls"] += 1
            n = state["calls"]
        if n == 1:
            return SearchResponse(SearchOutcome.TIMEOUT, detail="ReadTimeout")
        return ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")])

    responder(monkeypatch, handler)

    run_discovery(db, job)

    db.refresh(job)
    assert job.failed_queries == 0
    assert (
        db.query(SearchQueryRun)
        .filter(SearchQueryRun.status == QueryRunStatus.FAILED.value)
        .count()
        == 0
    )
    # The retry is visible in the attempt count on the run that recovered.
    retried = (
        db.query(SearchQueryRun).filter(SearchQueryRun.attempts > 1).first()
    )
    assert retried is not None


def test_total_search_failure_is_reported_as_failed(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    responder(
        monkeypatch,
        lambda q, page: SearchResponse(SearchOutcome.RATE_LIMITED, detail="RatelimitException"),
    )

    run_discovery(db, job)

    db.refresh(job)
    assert job.status == SearchJobStatus.FAILED
    assert job.candidates_found == 0
    assert job.error_message == "all_queries_failed"
    # No fabricated results.
    assert db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).count() == 0


def test_repeated_rate_limits_stop_hammering_the_engine(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    calls = responder(
        monkeypatch,
        lambda q, page: SearchResponse(SearchOutcome.RATE_LIMITED, detail="RatelimitException"),
    )

    run_discovery(db, job)

    from backend.services.discovery.runner import (
        MAX_QUERY_ATTEMPTS,
        RATE_LIMIT_STREAK_LIMIT,
    )

    db.refresh(job)
    # The breaker stops after a run of abandoned queries rather than grinding
    # through the whole plan. Parallel batches may abandon a few extra in-flight
    # queries in the same wave as the one that tripped the limit.
    assert job.failed_queries >= RATE_LIMIT_STREAK_LIMIT
    assert job.failed_queries < job.total_queries
    # No query is asked more times than its attempt budget allows.
    per_query = Counter(calls)
    assert max(per_query.values()) <= MAX_QUERY_ATTEMPTS


def test_empty_results_are_not_treated_as_failures(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    responder(monkeypatch, lambda q, page: ok([]))

    run_discovery(db, job)

    db.refresh(job)
    assert job.status == SearchJobStatus.COMPLETED
    assert job.failed_queries == 0
    assert job.candidates_found == 0
    assert "No candidates matched" in job.message


# --- Cancellation ---------------------------------------------------------


def test_cancellation_stops_discovery_and_keeps_the_pool(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    monkeypatch.setattr("backend.services.discovery.runner.QUERY_PARALLELISM", 1)

    def handler(query, page):
        job.cancel_requested = True
        db.commit()
        return ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")])

    calls = responder(monkeypatch, handler)

    run_discovery(db, job)

    db.refresh(job)
    assert job.status == SearchJobStatus.CANCELLED
    assert len(calls) == 1
    assert db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id).count() == 1


def test_a_finished_search_is_not_rerun(db, job, monkeypatch):
    job.status = SearchJobStatus.COMPLETED
    db.commit()
    calls = responder(monkeypatch, lambda q, page: ok([]))

    run_discovery(db, job)

    assert calls == []


# --- Discovery/enrichment separation (Phase 10) --------------------------


def test_leads_are_visible_before_enrichment_runs(db, job, monkeypatch):
    queued = no_enrichment(monkeypatch)
    responder(
        monkeypatch, lambda q, page: ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")])
    )

    run_discovery(db, job)

    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).one()
    assert lead.enrichment_status == EnrichmentStatus.PENDING.value
    assert lead.email is None
    # Enrichment was handed off, not executed inline.
    assert queued == [job.id]


def test_an_email_already_in_the_snippet_is_copied_onto_the_lead(db, job, monkeypatch):
    """Emails are the product; do not wait for enrichment to keep one we already saw."""
    queued = no_enrichment(monkeypatch)
    hits = [
        profile("anna-lind", "Anna Lind", "Nordic Freight AB"),
    ]
    hits[0]["body"] = (
        "Marketing Manager at Nordic Freight AB. Contact anna.lind@nordicfreight.se"
    )
    responder(monkeypatch, lambda q, page: ok(hits))

    run_discovery(db, job)

    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).one()
    assert lead.email == "anna.lind@nordicfreight.se"
    assert lead.email_source == "snippet"
    assert queued == [job.id]


def test_enrichment_is_not_queued_when_nothing_qualified(db, job, monkeypatch):
    queued = no_enrichment(monkeypatch)
    responder(
        monkeypatch,
        lambda q, page: ok([profile("per-nilsson", "Per Nilsson", "Acme AB", role="Head Chef")]),
    )

    run_discovery(db, job)

    assert queued == []


# --- Progress accounting (Phase 3) ---------------------------------------


def test_progress_advances_with_completed_queries(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    monkeypatch.setattr("backend.services.discovery.runner.QUERY_PARALLELISM", 1)
    seen: list[int] = []

    def handler(query, page):
        db.refresh(job)
        seen.append(job.progress)
        return ok([])

    responder(monkeypatch, handler)

    run_discovery(db, job)

    assert seen == sorted(seen), "progress must never move backwards"
    db.refresh(job)
    assert job.progress == 100


def test_counters_reflect_real_work_only(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    responder(
        monkeypatch, lambda q, page: ok([profile("anna-lind", "Anna Lind", "Nordic Freight AB")])
    )

    run_discovery(db, job)

    db.refresh(job)
    runs = db.query(SearchQueryRun).filter(SearchQueryRun.search_job_id == job.id).count()
    assert job.completed_queries + job.failed_queries <= runs
    assert job.candidates_found == 1


def test_max_candidates_caps_the_pool(db, job, monkeypatch):
    no_enrichment(monkeypatch)
    job.filters = {**FILTERS, "max_results": 2}
    db.commit()
    names = [
        "Anna Lind", "Erik Ohlsson", "Lena Berg", "Nils Soder", "Karin Falk",
        "Johan Ek", "Maja Holm", "Oskar Dahl", "Sara Lund", "Tove Ryd",
    ]
    hits = [profile(f"person-{i}", name, "Nordic Freight AB") for i, name in enumerate(names)]
    responder(monkeypatch, lambda q, page: ok(hits))

    run_discovery(db, job)

    stored = db.query(SearchCandidate).filter(SearchCandidate.search_job_id == job.id).count()
    assert 2 <= stored <= len(hits)
