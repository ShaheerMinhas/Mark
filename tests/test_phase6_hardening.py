"""Phase 6 controls that run on the default SQLite database. No network and no paid call."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from backend.models import (
    DiscoveryRun,
    DiscoverySettings,
    DiscoveryTarget,
    GlobalCompany,
    GlobalEvidence,
    GlobalPerson,
    LeadRecord,
    SearchJob,
    SearchJobStatus,
    User,
)
from backend.observability import log_event
from backend.services.crypto import decrypt_value, encrypt_value, reencrypt_provider_secrets
from backend.services.discovery_engine.heartbeat import beat, worker_health
from backend.services.discovery_engine.ingest import collect_public_hits
from backend.services.discovery_engine.pipeline import execute_run
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.discovery_engine.spend import seed_providers, set_credential
from backend.services.discovery_engine.ssrf import SsrfError, safe_fetch
from backend.services.discovery_engine.suppression import apply_retention
from backend.services.global_cache.serve import serve_cached_people
from backend.services.global_cache.upsert import PrivateEvidenceError, PublicProspect, upsert_public_prospect


def _client():
    from backend.main import app

    return TestClient(app)


def _register(client, email, *, organization=False):
    body = {"email": email, "password": "test-password-123", "full_name": "Phase Six"}
    if organization:
        body["account_type"] = "organization"
        body["organization_name"] = "Owned Workspace"
    created = client.post("/api/auth/register", json=body)
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['access_token']}"}, created.json()["user"]


def test_ssrf_guard_checks_resolution_and_redirects():
    seen = []

    def resolver(host):
        if host == "evil.example":
            return ["8.8.8.8", "127.0.0.1"]
        if host == "ok.example":
            return ["1.1.1.1"]
        if host == "meta.example":
            return ["169.254.169.254"]
        if host == "v6.example":
            return ["fd00::1"]
        return ["203.0.113.30"]

    def opener(url, addresses, timeout, max_bytes):
        seen.append((url, tuple(addresses)))
        if url.endswith("/go"):
            return 302, "https://evil.example/next", b""
        return 200, None, b"ok"

    try:
        safe_fetch("https://ok.example/go", resolver=resolver, opener=opener)
        raised = False
    except SsrfError:
        raised = True
    assert raised
    assert seen[-1][1] == ("1.1.1.1",)
    for blocked in (
        "https://meta.example/",
        "https://v6.example/",
        "http://127.0.0.1/",
        "http://user:pass@ok.example/",
        "http://ok.example:22/",
        "file:///tmp/secret",
    ):
        try:
            safe_fetch(blocked, resolver=resolver, opener=opener)
            assert False, blocked
        except SsrfError:
            pass


def test_fixture_hits_reach_the_cache_and_a_customer_search(db, user, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("network search called")

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _boom)
    monkeypatch.setattr(
        "backend.services.discovery_engine.ingest.collect_public_hits",
        lambda target: (
            [
                {
                    "full_name": "Example Fixture Nora",
                    "title": "CTO",
                    "company_name": "Example Fixture Co",
                    "domain": "example-fixture.test",
                    "country_code": "SE",
                    "profile_url": "https://www.linkedin.com/in/example-fixture-nora",
                    "email": "nora@example-fixture.test",
                    "email_status": "found",
                    "source_url": "http://127.0.0.1/private",
                    "phone": "555",
                    "phone_evidenced": False,
                },
                {
                    "full_name": "Example Fixture Nora",
                    "title": "CTO",
                    "company_name": "Example Fixture Co",
                    "domain": "example-fixture.test",
                    "country_code": "SE",
                    "profile_url": "https://www.linkedin.com/in/example-fixture-nora",
                },
            ],
            "fixture",
        ),
    )
    settings = ensure_settings(db)
    settings.enabled = True
    settings.allow_public_search = True
    db.commit()
    target = DiscoveryTarget(name="Example fixture source", scheduling_status="paused", countries=["SE"], titles=["cto"], autonomous=True)
    db.add(target)
    db.commit()
    run = DiscoveryRun(target_id=target.id, idempotency_key="fixture-e2e", status="queued", scheduled_for=datetime.utcnow())
    db.add(run)
    db.commit()
    execute_run(db, run)
    people = db.query(GlobalPerson).filter(GlobalPerson.profile_url_normalized.isnot(None)).all()
    assert len(people) == 1
    person = people[0]
    assert person.full_name == "Example Fixture Nora"
    assert person.phone is None
    assert person.company_name == "Example Fixture Co"
    assert db.query(GlobalCompany).count() == 1
    evidence = db.query(GlobalEvidence).filter(GlobalEvidence.subject_id == person.id).all()
    assert evidence
    assert all(row.source_url != "http://127.0.0.1/private" for row in evidence)
    job = SearchJob(
        name="Example customer",
        prompt="CTO",
        filters={"location": "Sweden", "job_titles": ["CTO"], "max_results": 5},
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    coverage = serve_cached_people(db, job)
    assert coverage.served == 1
    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).one()
    assert lead.name == "Example Fixture Nora"
    assert not getattr(lead, "notes", None)
    quiet = DiscoveryTarget(name="Quiet", autonomous=False)
    empty, detail = collect_public_hits(quiet)
    assert empty == []
    assert "No request was sent" in detail


def test_allowed_intent_still_blocks_the_network(db, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("network search called")

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _boom)
    settings = ensure_settings(db)
    settings.enabled = True
    settings.allow_public_search = True
    db.commit()
    target = DiscoveryTarget(name="Example blocked", scheduling_status="paused")
    db.add(target)
    db.commit()
    run = DiscoveryRun(target_id=target.id, idempotency_key="blocked-net", status="queued", scheduled_for=datetime.utcnow())
    db.add(run)
    db.commit()
    execute_run(db, run)
    from backend.models import DiscoveryStageAttempt

    discover = (
        db.query(DiscoveryStageAttempt)
        .filter(DiscoveryStageAttempt.run_id == run.id, DiscoveryStageAttempt.stage == "discover")
        .one()
    )
    assert discover.status == "skipped"
    assert "No request was sent" in discover.detail
    enrich = (
        db.query(DiscoveryStageAttempt)
        .filter(DiscoveryStageAttempt.run_id == run.id, DiscoveryStageAttempt.stage == "enrich")
        .one()
    )
    assert enrich.status in {"skipped", "unavailable"}


def test_retention_suppresses_the_cache_and_leaves_workspace_leads(db, user):
    person = upsert_public_prospect(
        db,
        PublicProspect(
            full_name="Example Old Nora",
            title="CTO",
            company_name="Example Old",
            domain="example-old.test",
            country_code="SE",
            profile_url="https://www.linkedin.com/in/example-old-nora",
            email="old@example-old.test",
        ),
    )
    person.discovered_at = datetime.utcnow() - timedelta(days=30)
    private_job = SearchJob(name="Private", prompt="private", filters={}, owner_id=user.id, status=SearchJobStatus.COMPLETED)
    db.add(private_job)
    db.commit()
    db.add(LeadRecord(search_job_id=private_job.id, name="Private Anna", email="anna@private.test"))
    settings = ensure_settings(db)
    settings.retention_days = 7
    db.commit()
    private_before = db.query(LeadRecord).count()
    assert apply_retention(db) == 1
    db.refresh(person)
    assert person.archived_at is not None
    assert db.query(LeadRecord).count() == private_before
    try:
        upsert_public_prospect(
            db,
            PublicProspect(
                full_name="Example Old Nora",
                title="CTO",
                company_name="Example Old",
                domain="example-old.test",
                profile_url="https://www.linkedin.com/in/example-old-nora",
                email="old@example-old.test",
            ),
        )
        assert False
    except PrivateEvidenceError:
        pass


def test_customers_cannot_read_operator_cost_or_another_workspace(db):
    seed_providers(db)
    set_credential(db, provider_name="hunter", api_key="platform-secret-value")
    db.commit()
    with _client() as client:
        member, _user = _register(client, "phase6-member@example.com")
        owner, _owner = _register(client, "phase6-owner@example.com", organization=True)
        other, other_user = _register(client, "phase6-other@example.com")
        created = client.post(
            "/api/searches",
            json={"name": "Example private", "prompt": "CTOs in Sweden", "filters": {"max_results": 10}},
            headers=member,
        )
        assert created.status_code == 200, created.text
        search_id = created.json()["search"]["id"]
        assert client.get(f"/api/searches/{search_id}", headers=other).status_code == 404
        for headers in (member, owner):
            assert client.get("/api/platform/discovery/usage", headers=headers).status_code == 401
            assert client.get("/api/platform/discovery/providers", headers=headers).status_code == 401
            assert client.get("/api/platform/discovery/people/export", headers=headers).status_code == 401
        cache = client.get("/api/cache/people", headers=member)
        assert cache.status_code == 200
        assert "platform-secret-value" not in cache.text
        assert "max_cost_minor" not in cache.text
        assert client.get("/api/platform/discovery/people?page_size=1000", headers=member).status_code == 401
        admin = db.query(User).filter(User.email == "phase6-member@example.com").one()
        admin.is_platform_admin = True
        db.commit()
        logged = client.post(
            "/api/auth/admin/login",
            json={"email": "phase6-member@example.com", "password": "test-password-123"},
        )
        assert logged.status_code == 200, logged.text
        admin_headers = {"Authorization": f"Bearer {logged.json()['access_token']}"}
        too_big = client.get("/api/platform/discovery/people?page_size=1000", headers=admin_headers)
        assert too_big.status_code == 422
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["database"] == "ok"
        assert health.json()["discovery_worker"] == "unknown"
        assert "platform-secret-value" not in health.text
        del other_user


def test_heartbeat_is_independent_of_the_planner_lease(db):
    assert worker_health(db)["status"] == "unknown"
    beat(db, "worker-a", kind="discovery", status="running")
    assert worker_health(db)["status"] == "running"
    row_time = datetime.utcnow() - timedelta(seconds=600)
    from backend.models import WorkerHeartbeat

    row = db.get(WorkerHeartbeat, "worker-a")
    row.beat_at = row_time
    db.commit()
    assert worker_health(db)["status"] == "stale"
    beat(db, "worker-a", status="stopping")
    assert worker_health(db)["status"] == "stopping"


def test_secret_redaction_rotation_and_unreadable_credentials(db):
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("mark.test.redact")
    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_event(logger, "provider.call", detail="api_key=platform-secret-value", note="sk-live-abcdef123456")
    finally:
        logger.removeHandler(handler)
    assert records
    assert "platform-secret-value" not in records[0]
    assert "sk-live-abcdef123456" not in records[0]
    seed_providers(db)
    set_credential(db, provider_name="hunter", api_key="rotate-me")
    db.commit()
    from backend.config import SESSION_ENCRYPTION_KEY
    from backend.models import DiscoveryProviderSecret

    assert decrypt_value(db.get(DiscoveryProviderSecret, "hunter").ciphertext)
    changed = reencrypt_provider_secrets(db, old_secret=SESSION_ENCRYPTION_KEY, new_secret="next-session-key")
    assert changed == 1
    from backend.services.crypto import _fernet_for

    plain = _fernet_for("next-session-key").decrypt(db.get(DiscoveryProviderSecret, "hunter").ciphertext.encode()).decode()
    assert "rotate-me" in plain
    try:
        reencrypt_provider_secrets(db, old_secret="wrong-key", new_secret="another")
        assert False
    except Exception:
        db.rollback()
    assert "rotate-me" in _fernet_for("next-session-key").decrypt(
        db.get(DiscoveryProviderSecret, "hunter").ciphertext.encode()
    ).decode()
    row = db.get(DiscoveryProviderSecret, "hunter")
    row.ciphertext = "not-a-token"
    db.commit()
    from backend.services.discovery_engine.spend import gate_reason
    from backend.models import DiscoveryProvider

    provider = db.get(DiscoveryProvider, "hunter")
    provider.enabled = True
    provider.rate_per_hour = 5
    db.commit()
    assert gate_reason(db, provider, "email_find") == "credential unreadable"


def test_representative_filter_stays_within_a_local_bound(db):
    now = datetime.utcnow()
    db.bulk_save_objects(
        [
            GlobalPerson(
                full_name=f"Example Person {index}",
                name_normalized=f"example person {index}",
                title="CTO",
                title_normalized="cto",
                country_code="SE" if index % 2 == 0 else "US",
                discovered_at=now,
                updated_at=now,
            )
            for index in range(2000)
        ]
    )
    db.commit()
    started = datetime.utcnow()
    count = db.query(GlobalPerson).filter(GlobalPerson.country_code == "SE", GlobalPerson.title_normalized == "cto").count()
    elapsed = (datetime.utcnow() - started).total_seconds()
    assert count == 1000
    assert elapsed < 5, elapsed
