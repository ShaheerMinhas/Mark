"""Phase 17 — structured logging, trace ids, and redaction."""

from __future__ import annotations

import logging

from backend.observability import log_event, new_trace_id


def test_trace_ids_are_short_and_unique():
    ids = {new_trace_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(value) == 16 for value in ids)


def test_log_event_emits_key_value_pairs(caplog):
    logger = logging.getLogger("test.obs")
    with caplog.at_level(logging.INFO, logger="test.obs"):
        log_event(logger, "discovery.query", search_id=7, candidates=12)

    message = caplog.records[0].getMessage()
    assert "event=discovery.query" in message
    assert "search_id=7" in message
    assert "candidates=12" in message


def test_log_event_skips_none_fields(caplog):
    logger = logging.getLogger("test.obs")
    with caplog.at_level(logging.INFO, logger="test.obs"):
        log_event(logger, "discovery.query", search_id=7, error=None)

    assert "error=" not in caplog.records[0].getMessage()


def test_log_event_redacts_sensitive_field_names(caplog):
    logger = logging.getLogger("test.obs")
    with caplog.at_level(logging.INFO, logger="test.obs"):
        log_event(
            logger,
            "provider.call",
            api_key="sk-live-abcdef123456",
            access_token="jwt.value.here",
            user_password="hunter2",
            provider="hunter",
        )

    message = caplog.records[0].getMessage()
    assert "sk-live-abcdef123456" not in message
    assert "jwt.value.here" not in message
    assert "hunter2" not in message
    assert "provider=hunter" in message


def test_log_event_respects_the_requested_level(caplog):
    logger = logging.getLogger("test.obs")
    with caplog.at_level(logging.WARNING, logger="test.obs"):
        log_event(logger, "query.failed", level=logging.WARNING, reason="timeout")

    assert caplog.records[0].levelno == logging.WARNING


def test_values_with_spaces_stay_parseable(caplog):
    logger = logging.getLogger("test.obs")
    with caplog.at_level(logging.INFO, logger="test.obs"):
        log_event(logger, "search.done", message="2 relevant leads found")

    message = caplog.records[0].getMessage()
    assert "event=search.done" in message
    assert "2 relevant leads found" in message


def test_discovery_logs_carry_the_job_trace_id(db, user, monkeypatch, caplog):
    from backend.models import SearchJob, SearchJobStatus
    from backend.services.ddg import SearchOutcome, SearchResponse
    from backend.services.discovery.runner import run_discovery
    from tests.conftest import serp_hit

    job = SearchJob(
        name="Traced",
        prompt="Marketing managers in Sweden",
        filters={"job_titles": ["Marketing Manager"], "active_only": False},
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
        trace_id="abc123def456abcd",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    monkeypatch.setattr(
        "backend.services.discovery.runner.search_text",
        lambda q, **kw: SearchResponse(
            SearchOutcome.OK,
            hits=[serp_hit("anna-lind", "Anna Lind", "Marketing Manager", "Nordic AB")],
        ),
    )
    monkeypatch.setattr("backend.services.search_jobs.enqueue_enrichment", lambda *a, **k: None)

    with caplog.at_level(logging.INFO):
        run_discovery(db, job)

    traced = [r for r in caplog.records if "abc123def456abcd" in r.getMessage()]
    assert traced, "no discovery log line carried the job trace id"
    events = " ".join(r.getMessage() for r in caplog.records)
    assert "discovery.complete" in events


def test_discovery_summary_log_reports_the_funnel(db, user, monkeypatch, caplog):
    from backend.models import SearchJob, SearchJobStatus
    from backend.services.ddg import SearchOutcome, SearchResponse
    from backend.services.discovery.runner import run_discovery
    from tests.conftest import serp_hit

    job = SearchJob(
        name="Funnel log",
        prompt="Marketing managers in Sweden",
        filters={"job_titles": ["Marketing Manager"], "active_only": False},
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
        trace_id=new_trace_id(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # The same hit on every query, so duplicates are guaranteed.
    hit = serp_hit("anna-lind", "Anna Lind", "Marketing Manager", "Nordic AB")
    monkeypatch.setattr(
        "backend.services.discovery.runner.search_text",
        lambda q, **kw: SearchResponse(SearchOutcome.OK, hits=[hit]),
    )
    monkeypatch.setattr("backend.services.search_jobs.enqueue_enrichment", lambda *a, **k: None)

    with caplog.at_level(logging.INFO):
        run_discovery(db, job)

    def event(name: str) -> str:
        return next(
            r.getMessage() for r in caplog.records if f"event={name}" in r.getMessage()
        )

    discovery = event("discovery.complete")
    for field in (
        "queries_attempted",
        "queries_succeeded",
        "queries_failed",
        "candidates",
        "duplicates",
        "duration_ms",
    ):
        assert field in discovery, f"{field} missing from the discovery summary log"

    qualification = event("qualification.complete")
    for field in ("evaluated", "qualified", "rejected", "promoted"):
        assert field in qualification, f"{field} missing from the qualification summary log"

    # Every query returned the same person, so duplicates were actually suppressed.
    db.refresh(job)
    assert job.candidates_deduplicated > 0
    assert job.candidates_found == 1
