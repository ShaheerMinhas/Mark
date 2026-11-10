"""Phase 1 shared cache: dedup, isolation, matching, and search fallback."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.models import GlobalCompany, GlobalPerson, LeadRecord, SearchJob, SearchJobStatus
from backend.services.discovery.runner import run_discovery
from backend.services.global_cache.match import CacheQuery, match_people
from backend.services.global_cache.upsert import (
    PrivateEvidenceError,
    PublicProspect,
    refuse_workspace_lead,
    upsert_public_prospect,
)


def _prospect(**overrides) -> PublicProspect:
    data = dict(
        full_name="Ada Found",
        title="CTO",
        company_name="Example Solar",
        domain="example-solar.test",
        industry="Software",
        country_code="Sweden",
        city="Stockholm",
        profile_url="https://www.linkedin.com/in/ada-found",
        email="ada@example-solar.test",
        email_status="found",
        email_confidence="medium",
        source_url="https://example-solar.test/team",
        provider="public_web",
        technologies=["Postgres"],
    )
    data.update(overrides)
    return PublicProspect(**data)


@pytest.fixture
def client(db):
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_unknown_fields_stay_null_and_inferred_email_is_not_verified(db):
    person = upsert_public_prospect(
        db,
        _prospect(email_status="inferred", phone="555", phone_evidenced=False, industry=None),
    )
    assert person.industry is None
    assert person.phone is None
    assert person.email_status == "inferred"
    assert person.quality_tier != "verified"
    assert person.quality_tier != "contactable"
    assert person.country_code == "SE"
    assert person.seniority == "c-level"


def test_same_domain_reuses_one_company(db):
    upsert_public_prospect(db, _prospect())
    upsert_public_prospect(
        db,
        _prospect(
            full_name="Bea Other",
            profile_url="https://www.linkedin.com/in/bea-other",
            email="bea@example-solar.test",
            email_status="verified",
        ),
    )
    assert db.query(GlobalCompany).count() == 1
    assert db.query(GlobalPerson).count() == 2
    verified = db.query(GlobalPerson).filter(GlobalPerson.email_status == "verified").one()
    assert verified.quality_tier == "verified"
    assert verified.company_id is not None


def test_conflicting_identity_is_not_merged(db):
    first = upsert_public_prospect(db, _prospect())
    second = upsert_public_prospect(
        db,
        _prospect(
            full_name="Cy Different",
            profile_url="https://se.linkedin.com/in/cy-different",
            email="ada@example-solar.test",
            email_status="found",
        ),
    )
    assert first.id != second.id
    assert db.query(GlobalPerson).count() == 2
    db.refresh(first)
    assert first.email == "ada@example-solar.test"
    assert second.email is None


def test_upsert_is_idempotent(db):
    first = upsert_public_prospect(db, _prospect())
    second = upsert_public_prospect(db, _prospect())
    assert first.id == second.id
    assert db.query(GlobalPerson).count() == 1


def test_workspace_lead_is_refused(db, user):
    job = SearchJob(
        name="Private",
        prompt="private notes",
        filters={},
        owner_id=user.id,
        status=SearchJobStatus.COMPLETED,
    )
    db.add(job)
    db.flush()
    lead = LeadRecord(
        search_job_id=job.id,
        name="Private Pat",
        email="pat@secret.test",
        email_status="verified",
        crm_notes="call on Tuesday",
        tags=["vip"],
    )
    db.add(lead)
    db.commit()
    with pytest.raises(PrivateEvidenceError):
        refuse_workspace_lead(lead)
    assert db.query(GlobalPerson).count() == 0


def test_matching_is_paged_in_sql(db):
    upsert_public_prospect(db, _prospect())
    upsert_public_prospect(
        db,
        _prospect(
            full_name="Dee Berlin",
            profile_url="https://www.linkedin.com/in/dee-berlin",
            email="dee@example-wind.test",
            email_status="not_found",
            domain="example-wind.test",
            company_name="Example Wind",
            country_code="DE",
            title="Engineer",
            technologies=[],
        ),
    )
    total, rows = match_people(db, CacheQuery(country_codes=["SE"], page=1, page_size=1))
    assert total == 1
    assert rows[0].full_name == "Ada Found"
    tech_total, tech_rows = match_people(db, CacheQuery(technologies=["postgres"]))
    assert tech_total == 1
    assert tech_rows[0].country_code == "SE"


def test_cache_hit_skips_live_search_when_coverage_is_enough(db, user, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("live search should not run")

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _boom)
    monkeypatch.setattr("backend.services.search_jobs.enqueue_enrichment", lambda *_a, **_k: None)
    upsert_public_prospect(db, _prospect())
    job = SearchJob(
        name="Cache only",
        prompt="CTO in Sweden",
        filters={"location": "Sweden", "job_titles": ["CTO"], "max_results": 1, "enrichment_mode": "selected"},
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    run_discovery(db, job)
    db.refresh(job)
    assert job.status == SearchJobStatus.COMPLETED
    assert job.cache_served == 1
    assert "Live search was not needed" in (job.message or "")
    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).one()
    assert lead.email_status == "found"
    assert lead.crm_notes is None
    assert db.query(GlobalPerson).count() == 1


def test_thin_cache_still_falls_back_to_live_search(db, user, monkeypatch):
    calls = {"n": 0}

    def _empty(*_args, **_kwargs):
        calls["n"] += 1
        from backend.services.ddg import SearchOutcome, SearchResponse

        return SearchResponse(SearchOutcome.EMPTY, hits=[])

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _empty)
    monkeypatch.setattr("backend.services.search_jobs.enqueue_enrichment", lambda *_a, **_k: None)
    upsert_public_prospect(db, _prospect())
    job = SearchJob(
        name="Needs more",
        prompt="CTO in Sweden",
        filters={"location": "Sweden", "job_titles": ["CTO"], "max_results": 3, "enrichment_mode": "selected"},
        owner_id=user.id,
        status=SearchJobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    run_discovery(db, job)
    db.refresh(job)
    assert calls["n"] > 0
    assert job.cache_served == 1
    assert db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).count() == 1


def test_cache_api_requires_login_and_hides_other_workspaces(client, db, user):
    upsert_public_prospect(db, _prospect())
    assert client.get("/api/cache/people").status_code == 401
    login = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "test-password-123"},
    )
    # The shared user fixture is not registered through the API password flow.
    if login.status_code != 200:
        client.post(
            "/api/auth/register",
            json={"email": "cache-reader@example.com", "password": "test-password-123", "full_name": "Cache Reader"},
        )
        login = client.post(
            "/api/auth/login",
            json={"email": "cache-reader@example.com", "password": "test-password-123"},
        )
    token = login.json()["access_token"]
    body = client.get("/api/cache/people?country=SE", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["total"] == 1
    assert "crm_notes" not in body["items"][0]
    assert body["items"][0]["email_status"] == "found"
