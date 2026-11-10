"""Phase 3 operator console APIs. Ordinary users are rejected by the server."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.models import DiscoveryRun, GlobalEvidence, GlobalPerson, QueuedJob, User
from backend.services.global_cache.upsert import PublicProspect, upsert_public_prospect


def _client(db):
    from backend.main import app

    return TestClient(app)


def _auth(client, email, *, organization=False):
    body = {"email": email, "password": "test-password-123", "full_name": "Admin Test"}
    if organization:
        body["account_type"] = "organization"
        body["organization_name"] = "Owned Workspace"
    created = client.post("/api/auth/register", json=body)
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['access_token']}"}, created.json()["user"]


def _admin(client, db, email="operator@example.com"):
    headers, user = _auth(client, email)
    row = db.query(User).filter(User.email == email).one()
    row.is_platform_admin = True
    db.commit()
    me = client.get("/api/auth/me", headers=headers).json()
    assert me["is_platform_admin"] is True
    return headers, user


def test_ordinary_users_cannot_read_the_operator_api(db):
    with _client(db) as client:
        member, _user = _auth(client, "member-admin@example.com")
        owner, _owner = _auth(client, "owner-admin@example.com", organization=True)
        for headers in (member, owner):
            assert client.get("/api/platform/discovery/overview", headers=headers).status_code == 403
            assert client.get("/api/platform/discovery/people", headers=headers).status_code == 403
            assert client.get("/api/platform/discovery/runs", headers=headers).status_code == 403


def test_overview_uses_real_rows_and_unknown_worker(db):
    upsert_public_prospect(
        db,
        PublicProspect(
            full_name="Example Ada",
            title="CTO",
            company_name="Example Solar",
            domain="example-solar.test",
            country_code="SE",
            email="ada@example-solar.test",
            email_status="found",
            profile_url="https://www.linkedin.com/in/example-ada",
        ),
    )
    with _client(db) as client:
        headers, _user = _admin(client, db)
        body = client.get("/api/platform/discovery/overview", headers=headers).json()
    assert body["counts"]["people"] == 1
    assert body["counts"]["companies"] == 1
    assert body["worker"]["status"] == "unknown"
    assert body["usage"]["estimated_cost"] is None
    assert body["usage"]["estimated_cost_status"] == "unavailable"
    assert body["engine"]["live_public_search"] == "disabled"
    assert body["engine"]["scheduling_enabled"] is False


def test_limits_persist_and_paid_switch_stays_off(db):
    with _client(db) as client:
        headers, _user = _admin(client, db, "limits@example.com")
        saved = client.patch(
            "/api/platform/discovery/settings",
            json={
                "enabled": False,
                "max_concurrency": 2,
                "max_runs_per_tick": 3,
                "max_runs_per_hour": 6,
                "max_runs_per_day": 10,
            },
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["paid_providers_enabled"] is False
        assert saved.json()["max_runs_per_hour"] == 6
        again = client.get("/api/platform/discovery/settings", headers=headers).json()
        assert again["max_concurrency"] == 2
        assert again["max_runs_per_day"] == 10


def test_pause_does_not_delete_a_queued_run_and_run_now_is_separate(db):
    with _client(db) as client:
        headers, _user = _admin(client, db, "sched@example.com")
        created = client.post(
            "/api/platform/discovery/targets",
            json={"name": "Example target", "schedule_type": "daily", "timezone": "UTC"},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        target_id = created.json()["id"]
        assert created.json()["scheduling_status"] == "paused"
        first = client.post(f"/api/platform/discovery/targets/{target_id}/run-now", headers=headers)
        assert first.status_code == 200, first.text
        assert db.query(DiscoveryRun).count() == 1
        paused = client.post(f"/api/platform/discovery/targets/{target_id}/pause", headers=headers)
        assert paused.json()["scheduling_status"] == "paused"
        assert db.query(DiscoveryRun).count() == 1
        assert db.query(QueuedJob).count() == 1


def test_archive_hides_from_the_customer_cache_and_keeps_evidence(db):
    person = upsert_public_prospect(
        db,
        PublicProspect(
            full_name="=Example Inject",
            title="CTO",
            company_name="Example Solar",
            domain="inject.test",
            country_code="US",
            email="inject@example.test",
            email_status="inferred",
            profile_url="https://www.linkedin.com/in/example-inject",
            source_url="https://inject.test/team",
        ),
    )
    with _client(db) as client:
        headers, _user = _admin(client, db, "archive@example.com")
        member, _member = _auth = _login_only(client, "reader-archive@example.com")
        listed = client.get("/api/cache/people?country=US", headers=member)
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        exported = client.get("/api/platform/discovery/people/export", headers=headers)
        assert exported.status_code == 200
        assert "'=Example Inject" in exported.text
        archived = client.post(f"/api/platform/discovery/people/{person.id}/archive", headers=headers)
        assert archived.status_code == 200
        db.expire_all()
        assert db.get(GlobalPerson, person.id).archived_at is not None
        assert db.query(GlobalEvidence).filter(GlobalEvidence.subject_id == person.id).count() >= 1
        hidden = client.get("/api/cache/people?country=US", headers=member).json()
        assert hidden["total"] == 0


def _login_only(client, email):
    client.post(
        "/api/auth/register",
        json={"email": email, "password": "test-password-123", "full_name": "Reader"},
    )
    token = client.post("/api/auth/login", json={"email": email, "password": "test-password-123"}).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}, email
