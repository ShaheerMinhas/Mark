"""Phase 2: schedules, leases, admin denial, and suppression. No live or paid calls."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.discovery_worker import claim_one, handle_claimed
from backend.models import (
    DiscoveryRun,
    DiscoverySettings,
    DiscoveryStageAttempt,
    DiscoveryTarget,
    GlobalPerson,
    JobType,
    QueuedJob,
    QueuedJobStatus,
    SearchJob,
    SearchJobStatus,
    User,
)
from backend.services.discovery_engine.backoff import RateLimited, backoff_seconds
from backend.services.discovery_engine.pipeline import execute_run
from backend.services.discovery_engine.schedule import next_run_at, plan_due_targets, try_acquire_planner
from backend.services.discovery_engine.settings import LIVE_PUBLIC_SEARCH_ENABLED, PAID_PROVIDERS_ENABLED, ensure_settings
from backend.services.discovery_engine.suppression import add_suppression
from backend.services.global_cache.match import CacheQuery, match_people
from backend.services.global_cache.serve import serve_cached_people
from backend.services.global_cache.upsert import PrivateEvidenceError, PublicProspect, upsert_public_prospect
from backend.services.jobqueue import queue


def _prospect(**overrides) -> PublicProspect:
    data = dict(
        full_name="Ada Found",
        title="CTO",
        company_name="Example Solar",
        domain="example-solar.test",
        country_code="Sweden",
        profile_url="https://www.linkedin.com/in/ada-found",
        email="ada@example-solar.test",
        email_status="found",
        source_url="https://example-solar.test/team",
    )
    data.update(overrides)
    return PublicProspect(**data)


def _target(db, **overrides) -> DiscoveryTarget:
    row = DiscoveryTarget(
        name=overrides.pop("name", "Sweden CTOs"),
        scheduling_status=overrides.pop("scheduling_status", "active"),
        schedule_type=overrides.pop("schedule_type", "daily"),
        timezone=overrides.pop("timezone", "UTC"),
        hour=overrides.pop("hour", 6),
        minute=overrides.pop("minute", 0),
        interval_minutes=overrides.pop("interval_minutes", None),
        countries=overrides.pop("countries", ["SE"]),
        titles=overrides.pop("titles", ["CTO"]),
        next_run_at=overrides.pop("next_run_at", datetime.utcnow() - timedelta(minutes=1)),
        max_people_per_run=overrides.pop("max_people_per_run", 25),
        max_queries_per_run=overrides.pop("max_queries_per_run", 5),
        refresh_hours=overrides.pop("refresh_hours", 168),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_backoff_is_capped_and_jitter_stays_inside_the_band():
    assert backoff_seconds(1, base=5, jitter=0) == 2.5
    assert backoff_seconds(1, base=5, jitter=1) == 5
    assert backoff_seconds(6, base=5, cap=20, jitter=1) == 20


def test_weekday_schedule_skips_the_weekend_in_the_target_timezone():
    target = DiscoveryTarget(
        name="Weekday",
        schedule_type="weekdays",
        timezone="America/New_York",
        hour=9,
        minute=0,
    )
    # Saturday 15:00 UTC is Saturday morning in New York.
    after = datetime(2026, 11, 14, 15, 0, 0)
    nxt = next_run_at(target, after)
    local = nxt.replace(tzinfo=__import__("datetime").timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    assert local.weekday() == 0
    assert local.hour == 9


def test_settings_default_off_and_paid_paths_stay_closed(db):
    row = ensure_settings(db)
    db.commit()
    assert row.enabled is False
    assert LIVE_PUBLIC_SEARCH_ENABLED is False
    assert PAID_PROVIDERS_ENABLED is False
    assert plan_due_targets(db, planner_id="planner-a") == 0
    assert db.query(DiscoveryRun).count() == 0


def test_duplicate_tick_creates_one_run_and_reopen_sees_it(db):
    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    _target(db)
    assert plan_due_targets(db, planner_id="planner-a") == 1
    assert plan_due_targets(db, planner_id="planner-a") == 0
    assert db.query(DiscoveryRun).count() == 1
    from backend.database import SessionLocal

    fresh = SessionLocal()
    try:
        assert fresh.query(DiscoveryRun).count() == 1
        assert fresh.query(QueuedJob).filter(QueuedJob.job_type == JobType.DISCOVERY_PIPELINE.value).count() == 1
    finally:
        fresh.close()


def test_pause_and_stop_keep_queued_runs(db):
    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    target = _target(db)
    plan_due_targets(db, planner_id="planner-a")
    queued = db.query(QueuedJob).count()
    target.scheduling_status = "paused"
    target.next_run_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    assert plan_due_targets(db, planner_id="planner-a") == 0
    target.scheduling_status = "stopped"
    db.commit()
    assert plan_due_targets(db, planner_id="planner-a") == 0
    assert db.query(DiscoveryRun).count() == 1
    assert db.query(QueuedJob).count() == queued


def test_cancel_marks_the_run_and_does_not_delete_the_queue_row(db):
    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    _target(db)
    plan_due_targets(db, planner_id="planner-a")
    run = db.query(DiscoveryRun).one()
    run.cancel_requested = True
    db.commit()
    execute_run(db, run)
    db.refresh(run)
    assert run.status == "cancelled"
    assert db.query(DiscoveryRun).count() == 1
    assert db.query(QueuedJob).count() == 1


def test_two_claimers_and_lease_recovery(db):
    queued = queue.enqueue(db, JobType.DISCOVERY_PIPELINE.value, {"run_id": 1}, max_attempts=3)
    first = queue.claim(db, worker_id="a", job_types=[JobType.DISCOVERY_PIPELINE.value])
    second = queue.claim(db, worker_id="b", job_types=[JobType.DISCOVERY_PIPELINE.value])
    assert first is not None and first.id == queued.id
    assert second is None
    row = db.get(QueuedJob, queued.id)
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    db.commit()
    assert queue.reclaim_expired(db) == 1
    recovered = queue.claim(db, worker_id="c", job_types=[JobType.DISCOVERY_PIPELINE.value])
    assert recovered is not None and recovered.id == queued.id


def test_concurrency_cap_refuses_a_second_running_job(db):
    settings = ensure_settings(db)
    settings.max_concurrency = 1
    db.add(
        QueuedJob(
            job_type=JobType.DISCOVERY_PIPELINE.value,
            payload={"run_id": 1},
            status=QueuedJobStatus.RUNNING.value,
        )
    )
    db.commit()
    assert claim_one(db, "worker") is None


def test_rate_limit_requeues_with_backoff_until_the_ceiling(db, monkeypatch):
    def _limited(*_args, **_kwargs):
        raise RateLimited("429")

    monkeypatch.setattr("backend.discovery_worker.execute_queued_job", _limited)
    queued = queue.enqueue(db, JobType.DISCOVERY_PIPELINE.value, {"run_id": 1}, max_attempts=2)
    handle = queue.claim(db, worker_id="a", job_types=[JobType.DISCOVERY_PIPELINE.value])
    handle_claimed(db, handle, jitter=0, heartbeat=False)
    row = db.get(QueuedJob, queued.id)
    assert row.status == QueuedJobStatus.QUEUED.value
    assert row.available_at > datetime.utcnow()
    handle = queue.claim(db, worker_id="b", job_types=[JobType.DISCOVERY_PIPELINE.value])
    # The retry is not available until backoff elapses.
    assert handle is None
    row.available_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    handle = queue.claim(db, worker_id="b", job_types=[JobType.DISCOVERY_PIPELINE.value])
    assert handle is not None and handle.is_final_attempt
    handle_claimed(db, handle, jitter=1, heartbeat=False)
    db.refresh(row)
    assert row.status == QueuedJobStatus.FAILED.value


def test_disabled_pipeline_does_not_call_public_or_paid_providers(db, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("provider called")

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _boom)
    monkeypatch.setattr("backend.services.EmailModule.finder.find_email", _boom)
    target = _target(db)
    run = DiscoveryRun(
        target_id=target.id,
        idempotency_key="manual-1",
        status="queued",
        scheduled_for=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    execute_run(db, run, attempt=1)
    db.refresh(run)
    assert run.status == "succeeded"
    details = {
        row.stage: row.detail
        for row in db.query(DiscoveryStageAttempt).filter(DiscoveryStageAttempt.run_id == run.id)
    }
    assert "No provider was called" in details["discover"]
    assert "Paid providers are disabled" in details["email"]
    assert "Paid providers are disabled" in details["enrich"]


def test_fresh_cache_skips_refresh(db, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("provider called")

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _boom)
    upsert_public_prospect(db, _prospect())
    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    target = _target(db, refresh_hours=168)
    run = DiscoveryRun(
        target_id=target.id,
        idempotency_key="fresh-1",
        status="queued",
        scheduled_for=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    execute_run(db, run)
    detail = (
        db.query(DiscoveryStageAttempt)
        .filter(DiscoveryStageAttempt.run_id == run.id, DiscoveryStageAttempt.stage == "discover")
        .one()
    )
    assert detail.status == "skipped_fresh"


def test_suppression_blocks_ingest_and_serving(db, user):
    upsert_public_prospect(db, _prospect())
    add_suppression(db, kind="email", value="ada@example-solar.test", reason="deleted", user_id=user.id)
    with pytest.raises(PrivateEvidenceError):
        upsert_public_prospect(db, _prospect(full_name="Ada Again", profile_url="https://www.linkedin.com/in/ada-again"))
    assert match_people(db, CacheQuery(country_codes=["SE"]))[0] == 0
    job = SearchJob(
        name="Serve",
        prompt="CTO",
        filters={"location": "Sweden", "job_titles": ["CTO"], "max_results": 5},
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    coverage = serve_cached_people(db, job)
    assert coverage.served == 0
    assert db.query(GlobalPerson).count() == 1


def test_planner_lock_is_exclusive_until_it_expires(db):
    assert try_acquire_planner(db, "a", lease_seconds=60) is True
    assert try_acquire_planner(db, "b", lease_seconds=60) is False
    from backend.models import SchedulerLock
    from backend.services.discovery_engine.schedule import SCHEDULER_LOCK

    row = db.get(SchedulerLock, SCHEDULER_LOCK)
    row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert try_acquire_planner(db, "b", lease_seconds=60) is True


@pytest.fixture
def client(db):
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def _auth(client, email: str, *, organization: bool = False) -> dict:
    body = {"email": email, "password": "test-password-123", "full_name": "Operator"}
    if organization:
        body["account_type"] = "organization"
        body["organization_name"] = "Example Org"
    created = client.post("/api/auth/register", json=body)
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['access_token']}"}


def test_customers_and_workspace_owners_cannot_configure_discovery(client, db):
    headers = _auth(client, "member@example.com")
    owner = _auth(client, "workspace-owner@example.com", organization=True)
    payload = {"name": "Secret crawl", "schedule_type": "daily", "timezone": "UTC"}
    assert client.post("/api/platform/discovery/targets", json=payload, headers=headers).status_code == 403
    assert client.post("/api/platform/discovery/targets", json=payload, headers=owner).status_code == 403
    user = db.query(User).filter(User.email == "workspace-owner@example.com").one()
    assert user.is_platform_admin is False
    user.is_platform_admin = True
    db.commit()
    created = client.post("/api/platform/discovery/targets", json=payload, headers=owner)
    assert created.status_code == 200, created.text
    assert created.json()["scheduling_status"] == "paused"
    settings = client.get("/api/platform/discovery/settings", headers=owner).json()
    assert settings["enabled"] is False
    assert settings["paid_providers_enabled"] is False


def test_overview_says_email_lookup_is_queued_instead_of_finished(client, db):
    headers = _auth(client, "overview@example.com")
    created = client.post(
        "/api/searches",
        json={"name": "Queued emails", "prompt": "CTOs in Sweden", "filters": {"location": "Sweden"}},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    search_id = created.json()["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.COMPLETED
    job.qualified_leads = 2
    db.add(
        QueuedJob(
            job_type=JobType.SEARCH_ENRICHMENT.value,
            payload={"search_job_id": search_id},
            status=QueuedJobStatus.QUEUED.value,
        )
    )
    db.commit()
    listed = client.get("/api/searches", headers=headers).json()
    match = next(item for item in listed if item["id"] == search_id)
    assert match["activity_label"] == "Email lookup queued"
    assert match["status"] == "completed"
