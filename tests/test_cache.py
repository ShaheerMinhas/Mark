"""Public-query result caching (Phase 13)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.models import SearchJob, SearchJobStatus, SearchQueryCache, SearchQueryRun
from backend.services.ddg import SearchOutcome, SearchResponse
from backend.services.discovery import cache
from backend.services.discovery.runner import run_discovery
from tests.conftest import serp_hit

HITS = [serp_hit("anna-lind", "Anna Lind", "Marketing Manager", "Nordic Freight AB")]


def test_store_then_read_round_trip(db):
    cache.store(db, query_hash="abc123", query_text="site:linkedin.com/in test", page=1, hits=HITS)

    assert cache.get_cached(db, "abc123") == HITS


def test_a_miss_returns_none(db):
    assert cache.get_cached(db, "never-stored") is None


def test_expired_entries_are_treated_as_misses(db):
    cache.store(db, query_hash="abc123", query_text="q", page=1, hits=HITS)
    row = db.query(SearchQueryCache).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()

    assert cache.get_cached(db, "abc123") is None
    assert db.query(SearchQueryCache).count() == 0


def test_empty_results_are_not_cached(db):
    """A transient zero-result response must not poison later searches."""
    cache.store(db, query_hash="abc123", query_text="q", page=1, hits=[])

    assert db.query(SearchQueryCache).count() == 0


def test_storing_the_same_query_twice_refreshes_one_row(db):
    cache.store(db, query_hash="abc123", query_text="q", page=1, hits=HITS)
    cache.store(db, query_hash="abc123", query_text="q", page=1, hits=HITS * 2)

    row = db.query(SearchQueryCache).one()
    assert row.hit_count == 2


def test_purge_removes_only_expired_rows(db):
    cache.store(db, query_hash="fresh", query_text="q", page=1, hits=HITS)
    cache.store(db, query_hash="stale", query_text="q", page=2, hits=HITS)
    stale = db.query(SearchQueryCache).filter(SearchQueryCache.query_hash == "stale").one()
    stale.expires_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()

    assert cache.purge_expired(db) == 1
    assert {row.query_hash for row in db.query(SearchQueryCache).all()} == {"fresh"}


def test_cache_key_contains_no_user_or_org_identity(db):
    """Two different users issuing the same public query share the SERP payload."""
    cache.store(db, query_hash="shared", query_text="q", page=1, hits=HITS)

    row = db.query(SearchQueryCache).one()
    columns = set(SearchQueryCache.__table__.columns.keys())
    assert "owner_id" not in columns and "organization_id" not in columns
    assert row.query_hash == "shared"


def test_cache_can_be_disabled(db, monkeypatch):
    monkeypatch.setattr(cache, "SEARCH_CACHE_HOURS", 0)

    cache.store(db, query_hash="abc123", query_text="q", page=1, hits=HITS)

    assert db.query(SearchQueryCache).count() == 0
    assert cache.get_cached(db, "abc123") is None


# --- Integration with discovery -------------------------------------------


@pytest.fixture
def job(db, user):
    record = SearchJob(
        name="Cached search",
        prompt="Marketing managers in Swedish logistics companies",
        filters={
            "job_titles": ["Marketing Manager"],
            "location": "Sweden",
            "industry": "logistics",
            "active_only": False,
        },
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
        trace_id="trace-cache",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def test_discovery_populates_the_cache(db, job, monkeypatch):
    monkeypatch.setattr(
        "backend.services.search_jobs.enqueue_enrichment", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "backend.services.discovery.runner.search_text",
        lambda q, **kw: SearchResponse(SearchOutcome.OK, hits=HITS),
    )

    run_discovery(db, job)

    assert db.query(SearchQueryCache).count() > 0


def test_a_cached_query_skips_the_search_engine(db, job, monkeypatch):
    monkeypatch.setattr(
        "backend.services.search_jobs.enqueue_enrichment", lambda *a, **k: None
    )
    from backend.services.discovery.queries import build_query_plan

    for spec in build_query_plan(job.prompt, dict(job.filters or {})):
        cache.store(
            db, query_hash=spec.hash, query_text=spec.text, page=spec.page, hits=HITS
        )
    calls: list[str] = []

    def fail_if_called(q, **kw):
        calls.append(q)
        return SearchResponse(SearchOutcome.EMPTY)

    monkeypatch.setattr("backend.services.discovery.runner.search_text", fail_if_called)

    run_discovery(db, job)

    db.refresh(job)
    assert calls == [], "cached queries must not reach the search engine"
    assert job.candidates_found == 1
    assert all(
        run.from_cache
        for run in db.query(SearchQueryRun)
        .filter(SearchQueryRun.search_job_id == job.id)
        .all()
        if run.status == "succeeded"
    )


def test_failed_queries_are_not_cached(db, job, monkeypatch):
    monkeypatch.setattr(
        "backend.services.search_jobs.enqueue_enrichment", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "backend.services.discovery.runner.search_text",
        lambda q, **kw: SearchResponse(SearchOutcome.RATE_LIMITED, detail="RatelimitException"),
    )

    run_discovery(db, job)

    assert db.query(SearchQueryCache).count() == 0
