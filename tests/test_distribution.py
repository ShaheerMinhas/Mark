"""Cache distribution, two-hour rotation, and replacement. No live network."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from backend.models import (
    DiscoveryRun,
    DiscoveryTarget,
    DistributionSlot,
    GlobalPerson,
    LeadNote,
    LeadRecord,
    Organization,
    OrganizationMember,
    OrgRole,
    SearchJob,
    SearchJobStatus,
    User,
)
from backend.services.discovery_engine.distribution import customer_key_for, set_max_customers, try_allocate
from backend.services.discovery_engine.pipeline import execute_run
from backend.services.discovery_engine.public_source import advance_cursor, hits_from_html, next_query
from backend.services.discovery_engine.schedule import plan_due_targets
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.global_cache.serve import serve_cached_people
from backend.services.global_cache.upsert import PublicProspect, upsert_public_prospect


def _client():
    from backend.main import app

    return TestClient(app)


def _person(db, slug, name):
    return upsert_public_prospect(
        db,
        PublicProspect(
            full_name=name,
            title="CTO",
            company_name=f"{name} Co",
            domain=f"{slug}.test",
            country_code="SE",
            profile_url=f"https://www.linkedin.com/in/{slug}",
            email=None,
        ),
    )


def _job(db, user, *, organization_id=None, name="Search"):
    job = SearchJob(
        name=name,
        prompt="CTO Sweden",
        filters={"location": "Sweden", "job_titles": ["CTO"], "max_results": 10},
        owner_id=user.id,
        organization_id=organization_id,
        status=SearchJobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _user(db, email):
    from backend.auth_utils import hash_password

    user = User(email=email, full_name=email, hashed_password=hash_password("test-password-123"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_rotation_changes_the_query_and_a_second_tick_does_not_duplicate(db, monkeypatch):
    calls = []

    def _fetch(target, **kwargs):
        calls.append(next_query(target))
        advance_cursor(target)
        return (
            [{
                "full_name": "Example Ada Found",
                "title": "CTO",
                "company_name": "Example Solar",
                "domain": "example-solar.test",
                "profile_url": "https://www.linkedin.com/in/example-ada-found",
                "email": None,
                "phone_evidenced": False,
            }],
            calls[-1],
        )

    monkeypatch.setattr("backend.services.discovery_engine.public_source.search_public_profiles", _fetch)
    monkeypatch.setattr(
        "backend.services.discovery_engine.ingest.collect_public_hits",
        lambda target: __import__(
            "backend.services.discovery_engine.public_source", fromlist=["search_public_profiles"]
        ).search_public_profiles(target),
    )
    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    target = DiscoveryTarget(
        name="Example technology",
        scheduling_status="active",
        schedule_type="interval",
        interval_minutes=120,
        autonomous=True,
        next_run_at=datetime.utcnow() - timedelta(minutes=1),
        criteria={
            "geography": {"include_countries": ["SE", "US"]},
            "titles": {"exact": ["cto"], "synonyms": ["chief technology officer"], "exclude": []},
            "technologies": {"mode": "optional", "include": ["aws", "azure"]},
            "industry": {"include": ["software"], "exclude": []},
        },
    )
    db.add(target)
    db.commit()
    assert plan_due_targets(db, planner_id="planner-a") == 1
    assert plan_due_targets(db, planner_id="planner-a") == 0
    db.refresh(target)
    assert target.next_run_at > datetime.utcnow() + timedelta(minutes=90)
    run = db.query(DiscoveryRun).one()
    execute_run(db, run)
    assert len(calls) == 1
    db.refresh(target)
    first = target.rotation_cursor
    execute_run(db, run)
    db.refresh(target)
    assert calls[0] != next_query(target) or first != target.rotation_cursor
    assert db.query(GlobalPerson).filter(GlobalPerson.full_name == "Example Ada Found").count() == 1
    page = (
        '<a href="https://www.linkedin.com/in/example-ada">Example Ada - CTO - Example Solar | LinkedIn</a>'
    )
    parsed = hits_from_html(page, query="cto sweden")
    assert parsed[0]["email"] is None
    assert parsed[0]["full_name"]


def test_three_customer_limit_teammates_and_replacement(db):
    person = _person(db, "shared-ada", "Shared Ada")
    other = _person(db, "other-bea", "Other Bea")
    owner = _user(db, "dist-owner@example.com")
    teammate = _user(db, "dist-teammate@example.com")
    outsider = _user(db, "dist-outsider@example.com")
    third = _user(db, "dist-third@example.com")
    fourth = _user(db, "dist-fourth@example.com")
    org = Organization(name="Shared Workspace", slug="shared-workspace")
    db.add(org)
    db.commit()
    db.add(OrganizationMember(organization_id=org.id, user_id=owner.id, role=OrgRole.OWNER))
    db.add(OrganizationMember(organization_id=org.id, user_id=teammate.id, role=OrgRole.ADMIN))
    db.commit()
    first = _job(db, owner, organization_id=org.id, name="Owner search")
    second = _job(db, teammate, organization_id=org.id, name="Teammate search")
    assert serve_cached_people(db, first).served == 2
    assert serve_cached_people(db, second).served == 2
    slot = db.get(DistributionSlot, person.canonical_key)
    assert slot.recipient_count == 1
    assert customer_key_for(first) == customer_key_for(second)
    again = serve_cached_people(db, first)
    assert again.served == 0
    assert db.get(DistributionSlot, person.canonical_key).recipient_count == 1
    serve_cached_people(db, _job(db, outsider, name="Outsider"))
    serve_cached_people(db, _job(db, third, name="Third"))
    assert db.get(DistributionSlot, person.canonical_key).recipient_count == 3
    missed = serve_cached_people(db, _job(db, fourth, name="Fourth"))
    assert missed.served == 0
    _person(db, "fresh-cio", "Fresh Cio")
    owned = db.query(LeadRecord).filter(LeadRecord.search_job_id == first.id, LeadRecord.name == "Shared Ada").one()
    db.add(LeadNote(lead_id=owned.id, user_id=owner.id, body="Keep this note"))
    db.commit()
    with _client() as client:
        logged = client.post("/api/auth/login", json={"email": owner.email, "password": "test-password-123"})
        headers = {"Authorization": f"Bearer {logged.json()['access_token']}"}
        stranger = client.post("/api/auth/login", json={"email": fourth.email, "password": "test-password-123"})
        stranger_headers = {"Authorization": f"Bearer {stranger.json()['access_token']}"}
        denied = client.post(f"/api/people/{owned.id}/replace", headers=stranger_headers)
        assert denied.status_code == 404
        replaced = client.post(f"/api/people/{owned.id}/replace", headers=headers)
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["status"] == "replaced"
        assert "recipient" not in replaced.text
    db.refresh(owned)
    assert owned.replaced_at is not None
    assert db.query(LeadNote).filter(LeadNote.lead_id == owned.id).count() == 1
    assert db.query(LeadRecord).filter(LeadRecord.search_job_id == first.id).count() == 3
    assert db.query(LeadRecord).filter(LeadRecord.id == owned.id, LeadRecord.name == "Shared Ada").count() == 1
    set_max_customers(db, 2)
    fifth = _job(db, _user(db, "dist-fifth@example.com"), name="Fifth")
    serve_cached_people(db, fifth)
    fifth_names = [row.name for row in db.query(LeadRecord).filter(LeadRecord.search_job_id == fifth.id)]
    assert "Shared Ada" not in fifth_names
    assert "Other Bea" not in fifth_names
    try:
        set_max_customers(db, 3)
        assert False
    except ValueError:
        pass
    set_max_customers(db, 3, confirm_increase=True)


def test_autonomous_search_still_runs_when_the_cache_is_fresh(db, monkeypatch):
    calls = []

    def _fetch(target):
        calls.append(next_query(target))
        advance_cursor(target)
        return [], "No identifiable public profile was parsed. Nothing was invented."

    monkeypatch.setattr("backend.services.discovery_engine.ingest.collect_public_hits", _fetch)
    _person(db, "already-fresh", "Already Fresh")
    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    target = DiscoveryTarget(
        name="Fresh technology",
        scheduling_status="active",
        schedule_type="interval",
        interval_minutes=120,
        autonomous=True,
        countries=["SE"],
        titles=["CTO"],
        refresh_hours=168,
        next_run_at=datetime.utcnow() - timedelta(minutes=1),
    )
    db.add(target)
    db.commit()
    run = DiscoveryRun(target_id=target.id, idempotency_key="fresh-auto", status="queued", scheduled_for=datetime.utcnow())
    db.add(run)
    db.commit()
    execute_run(db, run)
    assert calls


def test_a_later_profile_keeps_the_same_allowance(db):
    first = upsert_public_prospect(
        db,
        PublicProspect(
            full_name="Alias Ada",
            title="CTO",
            company_name="Alias Co",
            domain="alias-ada.test",
            country_code="SE",
            email=None,
        ),
    )
    assert first.canonical_key.startswith("name_domain:")
    old_key = first.canonical_key
    owners = [_user(db, f"alias-{index}@example.com") for index in range(3)]
    for owner in owners:
        assert try_allocate(
            db,
            canonical_key=first.canonical_key,
            customer_key=f"user:{owner.id}",
            person_id=first.id,
            search_job_id=None,
            lead_record_id=None,
        ) == "allocated"
    db.commit()
    updated = upsert_public_prospect(
        db,
        PublicProspect(
            full_name="Alias Ada",
            title="CTO",
            company_name="Alias Co",
            domain="alias-ada.test",
            country_code="SE",
            profile_url="https://www.linkedin.com/in/alias-ada",
            email=None,
        ),
    )
    assert updated.id == first.id
    assert updated.canonical_key.startswith("profile:")
    slot = db.get(DistributionSlot, updated.canonical_key)
    assert slot.recipient_count == 3
    assert db.get(DistributionSlot, old_key) is None
    missed = serve_cached_people(db, _job(db, _user(db, "alias-fourth@example.com"), name="Alias fourth"))
    assert missed.served == 0


def test_replace_live_search_is_one_guarded_query_and_stays_off(db, monkeypatch):
    calls = []

    def _fetch(target):
        calls.append(getattr(target, "id", None))
        return (
            [{
                "full_name": "Live Nia",
                "title": "CTO",
                "company_name": "Live Co",
                "domain": "live-nia.test",
                "country_code": "SE",
                "profile_url": "https://www.linkedin.com/in/live-nia",
                "email": None,
                "phone_evidenced": False,
                "source_url": "https://html.duckduckgo.com/html/?q=cto",
            }],
            "cto sweden",
        )

    monkeypatch.setattr("backend.services.discovery_engine.ingest.collect_public_hits", _fetch)
    owner = _user(db, "replace-owner@example.com")
    _person(db, "only-ada", "Only Ada")
    job = _job(db, owner, name="Only search")
    assert serve_cached_people(db, job).served == 1
    owned = db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).one()
    with _client() as client:
        logged = client.post("/api/auth/login", json={"email": owner.email, "password": "test-password-123"})
        headers = {"Authorization": f"Bearer {logged.json()['access_token']}"}
        blocked = client.post(f"/api/people/{owned.id}/replace", headers=headers)
        assert blocked.status_code == 200
        assert blocked.json()["status"] == "exhausted"
        assert calls == []
        settings = ensure_settings(db)
        settings.enabled = True
        db.commit()
        still = client.post(f"/api/people/{owned.id}/replace", headers=headers)
        assert still.json()["status"] == "exhausted"
        assert calls == []
        target = DiscoveryTarget(
            name="Live technology",
            scheduling_status="active",
            autonomous=True,
            schedule_type="interval",
            interval_minutes=120,
            criteria={"titles": {"exact": ["cto"], "synonyms": []}, "geography": {"include_countries": ["SE"]}},
        )
        db.add(target)
        db.commit()
        replaced = client.post(f"/api/people/{owned.id}/replace", headers=headers)
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["status"] == "replaced"
        assert len(calls) == 1
    db.refresh(owned)
    assert owned.name == "Only Ada"
    names = [row.name for row in db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id)]
    assert "Live Nia" in names
    assert "Only Ada" in names


def test_customer_token_cannot_change_the_distribution_limit(db):
    with _client() as client:
        created = client.post(
            "/api/auth/register",
            json={"email": "dist-customer@example.com", "password": "test-password-123", "full_name": "Customer"},
        )
        headers = {"Authorization": f"Bearer {created.json()['access_token']}"}
        assert client.patch("/api/platform/discovery/distribution", json={"max_customers": 2}, headers=headers).status_code == 401
