"""End-to-end: queued job -> worker -> discovery -> queued enrichment.

Proves the API hands work off and the worker completes it, with the search engine
and email providers stubbed out.
"""

from __future__ import annotations

import threading

import pytest

from backend.models import (
    EnrichmentStatus,
    JobType,
    LeadRecord,
    QueuedJob,
    QueuedJobStatus,
    SearchCandidate,
    SearchJob,
    SearchJobStatus,
)
from backend.services.ddg import SearchOutcome, SearchResponse
from backend.services.jobqueue import DbJobQueue
from tests.conftest import serp_hit

QUEUE = DbJobQueue()

def swedish_profile(slug: str, name: str, role: str, company: str) -> dict:
    return serp_hit(
        slug,
        name,
        role,
        company,
        body=f"{role} at {company}. Experience: {company}. Logistics. Stockholm, Sweden.",
    )


HITS = [
    swedish_profile("anna-lind", "Anna Lind", "Marketing Manager", "Nordic Freight AB"),
    swedish_profile("erik-ohlsson", "Erik Ohlsson", "Head of Marketing", "Baltic Logistics AB"),
]


@pytest.fixture
def queued_search(db, user):
    from backend.services.search_jobs import enqueue_discovery

    job = SearchJob(
        name="Worker run",
        prompt="Marketing managers in Swedish logistics companies",
        filters={
            "job_titles": ["Marketing Manager"],
            "location": "Sweden",
            "industry": "logistics",
            "active_only": False,
        },
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    enqueue_discovery(db, job)
    return job


@pytest.fixture
def stub_engine(monkeypatch):
    monkeypatch.setattr(
        "backend.services.discovery.runner.search_text",
        lambda q, **kw: SearchResponse(SearchOutcome.OK, hits=HITS),
    )


@pytest.fixture
def stub_enrichment(monkeypatch):
    """No company lookups, provider calls, or public-web crawling."""
    monkeypatch.setattr(
        "backend.services.discovery.enrichment._enrich_company", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "backend.services.EmailModule.intelligence.discover_public_emails",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.services.discovery.enrichment.discover_public_phones",
        lambda *a, **k: [],
    )


def drain(worker_types, limit=10):
    from backend import worker as worker_module

    worker = worker_module.Worker(job_types=worker_types)
    ran = 0
    while ran < limit and worker.poll_once():
        ran += 1
    return ran


def test_worker_picks_up_a_queued_search_and_completes_it(
    db, queued_search, stub_engine, stub_enrichment
):
    drain([JobType.SEARCH_DISCOVERY.value, JobType.SEARCH_ENRICHMENT.value])

    db.refresh(queued_search)
    assert queued_search.status in (
        SearchJobStatus.COMPLETED,
        SearchJobStatus.PARTIALLY_COMPLETED,
    )
    assert queued_search.candidates_found == 2
    assert queued_search.qualified_leads == 2
    assert db.query(LeadRecord).filter(LeadRecord.search_job_id == queued_search.id).count() == 2


def test_discovery_queues_enrichment_as_a_separate_job(db, queued_search, stub_engine):
    drain([JobType.SEARCH_DISCOVERY.value], limit=1)

    enrichment_jobs = (
        db.query(QueuedJob)
        .filter(QueuedJob.job_type == JobType.SEARCH_ENRICHMENT.value)
        .all()
    )
    assert len(enrichment_jobs) == 1
    assert enrichment_jobs[0].status == QueuedJobStatus.QUEUED.value


def test_leads_exist_before_the_enrichment_job_runs(db, queued_search, stub_engine):
    """The whole point of splitting the stages: results do not wait on providers."""
    drain([JobType.SEARCH_DISCOVERY.value], limit=1)

    leads = db.query(LeadRecord).filter(LeadRecord.search_job_id == queued_search.id).all()
    assert len(leads) == 2
    assert all(lead.enrichment_status == EnrichmentStatus.PENDING.value for lead in leads)


def test_the_discovery_queue_entry_is_marked_done(db, queued_search, stub_engine):
    drain([JobType.SEARCH_DISCOVERY.value], limit=1)

    entry = (
        db.query(QueuedJob)
        .filter(QueuedJob.job_type == JobType.SEARCH_DISCOVERY.value)
        .one()
    )
    assert entry.status == QueuedJobStatus.DONE.value
    assert entry.finished_at is not None


def test_a_crashing_discovery_job_is_retried_and_candidates_persist(
    db, queued_search, monkeypatch
):
    state = {"runs": 0}
    original_hits = list(HITS)
    lock = threading.Lock()

    def flaky(query, **kwargs):
        with lock:
            state["runs"] += 1
            n = state["runs"]
        if n == 2:
            raise RuntimeError("worker died mid-run")
        return SearchResponse(SearchOutcome.OK, hits=original_hits)

    monkeypatch.setattr("backend.services.discovery.runner.search_text", flaky)
    monkeypatch.setattr(
        "backend.services.search_jobs.enqueue_enrichment", lambda *a, **k: None
    )

    drain([JobType.SEARCH_DISCOVERY.value], limit=5)

    # Candidates from queries that finished before the crash stay in the pool.
    assert (
        db.query(SearchCandidate)
        .filter(SearchCandidate.search_job_id == queued_search.id)
        .count()
        == 2
    )


def test_enrichment_updates_counters_without_changing_the_verdict(
    db, queued_search, stub_engine, stub_enrichment
):
    drain([JobType.SEARCH_DISCOVERY.value], limit=1)
    db.refresh(queued_search)
    verdict = queued_search.status

    drain([JobType.SEARCH_ENRICHMENT.value], limit=1)

    db.refresh(queued_search)
    assert queued_search.status == verdict
    assert queued_search.enrichment_attempted == 2
    # Every lead reached a terminal enrichment state, even though no email was found.
    assert queued_search.enrichment_completed == 2
    assert queued_search.progress == 100


def test_one_failing_lead_does_not_discard_the_other_leads_enrichment(
    db, queued_search, stub_engine, monkeypatch
):
    """A provider blowing up on one lead must not roll back its batch siblings."""
    from backend.services.EmailModule.types import EmailResult, EmailStatus

    monkeypatch.setattr(
        "backend.services.discovery.enrichment._enrich_company", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "backend.services.discovery.enrichment.discover_public_phones",
        lambda *a, **k: [],
    )

    def selective(self, person, **kwargs):
        if person.name == "Anna Lind":
            raise RuntimeError("provider exploded")
        return EmailResult(
            email="erik@baltic-logistics.se",
            status=EmailStatus.FOUND.value,
            confidence=0.8,
            source="test",
        )

    monkeypatch.setattr(
        "backend.services.EmailModule.intelligence.EmailEnrichmentService.enrich", selective
    )

    drain([JobType.SEARCH_DISCOVERY.value, JobType.SEARCH_ENRICHMENT.value])

    by_name = {
        lead.name: lead
        for lead in db.query(LeadRecord).filter(
            LeadRecord.search_job_id == queued_search.id
        )
    }
    assert by_name["Anna Lind"].enrichment_status == EnrichmentStatus.FAILED.value
    assert by_name["Erik Ohlsson"].enrichment_status == EnrichmentStatus.ENRICHED.value
    assert by_name["Erik Ohlsson"].email == "erik@baltic-logistics.se"

    db.refresh(queued_search)
    assert queued_search.emails_found == 1
    assert queued_search.enrichment_completed == 1
    assert queued_search.errors_count >= 1


def test_counters_match_the_persisted_rows(db, queued_search, stub_engine, stub_enrichment):
    """Guards the autoflush=False trap: counters are read back from the database."""
    drain([JobType.SEARCH_DISCOVERY.value, JobType.SEARCH_ENRICHMENT.value])

    db.refresh(queued_search)
    terminal = (
        db.query(LeadRecord)
        .filter(
            LeadRecord.search_job_id == queued_search.id,
            LeadRecord.enrichment_status.in_(
                [EnrichmentStatus.ENRICHED.value, EnrichmentStatus.NO_MATCH.value]
            ),
        )
        .count()
    )
    assert queued_search.enrichment_completed == terminal


def test_worker_handles_a_deleted_search_job_gracefully(db, queued_search, stub_engine):
    db.delete(queued_search)
    db.commit()

    drain([JobType.SEARCH_DISCOVERY.value], limit=1)

    entry = (
        db.query(QueuedJob)
        .filter(QueuedJob.job_type == JobType.SEARCH_DISCOVERY.value)
        .one()
    )
    assert entry.status == QueuedJobStatus.DONE.value
