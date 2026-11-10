"""Phase 4 targeting rules. No provider calls and no invented people."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.database import SessionLocal
from backend.models import (
    DiscoveryFieldRule,
    DiscoveryRun,
    DiscoveryStageAttempt,
    DiscoveryTarget,
    GlobalEvidence,
    GlobalPerson,
    GlobalPersonTechnology,
    User,
)
from backend.services.discovery_engine.pipeline import execute_run
from backend.services.discovery_engine.schedule import plan_due_targets
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.discovery_engine.suppression import add_suppression
from backend.services.discovery_engine.targeting import apply_criteria, field_rules, seed_defaults
from backend.services.global_cache.match import CacheQuery, match_people
from backend.services.global_cache.upsert import PublicProspect, upsert_public_prospect


def _client(db):
    from backend.main import app

    return TestClient(app)


def _auth(client, email, *, organization=False):
    body = {"email": email, "password": "test-password-123", "full_name": "Target Test"}
    if organization:
        body["account_type"] = "organization"
        body["organization_name"] = "Owned Workspace"
    created = client.post("/api/auth/register", json=body)
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['access_token']}"}


def _admin(client, db, email):
    headers = _auth(client, email)
    row = db.query(User).filter(User.email == email).one()
    row.is_platform_admin = True
    db.commit()
    return headers


def _criteria(**overrides):
    criteria = {
        "geography": {
            "include_countries": ["SE", "US"],
            "exclude_countries": ["DE"],
            "include_regions": ["CA"],
            "include_cities": ["Stockholm"],
            "exclude_cities": ["Berlin"],
        },
        "industry": {"include": ["Software"], "exclude": ["Telecom"], "include_sub": [], "exclude_sub": []},
        "titles": {"exact": ["CTO"], "synonyms": ["Chief Technology Officer"], "exclude": ["intern"]},
        "functions": ["engineering"],
        "seniorities": ["c-level"],
        "departments": [],
        "company": {"employee_min": 50, "employee_max": 500, "sizes": [], "types": ["private"]},
        "technologies": {"mode": "optional", "include": ["AWS"]},
    }
    criteria.update(overrides)
    return criteria


def _save(db, prospect: PublicProspect):
    return upsert_public_prospect(db, prospect)


def test_preset_is_paused_and_creates_no_people(db):
    assert db.query(GlobalPerson).count() == 0
    seed_defaults(db)
    db.commit()
    preset = db.query(DiscoveryTarget).filter(DiscoveryTarget.name == "US and Europe technology leaders").one()
    assert preset.scheduling_status == "paused"
    assert preset.next_run_at is None
    assert "US" in preset.criteria["geography"]["include_countries"]
    assert "EU" not in preset.criteria["geography"]["include_countries"]
    assert preset.criteria["technologies"]["mode"] == "optional"
    assert db.query(GlobalPerson).count() == 0
    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    assert plan_due_targets(db, planner_id="planner-preset") == 0
    assert db.query(DiscoveryRun).count() == 0


def test_criteria_roundtrip_survives_a_new_session(db):
    seed_defaults(db)
    db.commit()
    with _client(db) as client:
        headers = _admin(client, db, "target-admin@example.com")
        created = client.post(
            "/api/platform/discovery/targets",
            json={"name": "Example Nordics", "schedule_type": "hourly", "timezone": "Europe/Stockholm", "criteria": _criteria()},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        target_id = created.json()["id"]
        assert created.json()["scheduling_status"] == "paused"
        assert created.json()["criteria"]["titles"]["exact"] == ["cto"]
        assert "chief technology officer" in created.json()["criteria"]["titles"]["synonyms"]
        updated = client.patch(
            f"/api/platform/discovery/targets/{target_id}",
            json={
                "name": "Example Nordics revised",
                "schedule_type": "weekdays",
                "timezone": "Europe/Stockholm",
                "hour": 8,
                "minute": 30,
                "priority": 20,
                "max_people_per_run": 40,
                "criteria": _criteria(),
            },
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
    fresh = SessionLocal()
    try:
        row = fresh.get(DiscoveryTarget, target_id)
        assert row.name == "Example Nordics revised"
        assert row.schedule_type == "weekdays"
        assert row.timezone == "Europe/Stockholm"
        assert row.criteria["geography"]["exclude_countries"] == ["DE"]
        assert row.criteria["technologies"]["include"] == ["aws"]
        assert row.scheduling_status == "paused"
    finally:
        fresh.close()


def test_matching_keeps_null_optional_technology_and_applies_exclusions(db):
    seed_defaults(db)
    db.commit()
    kept = _save(db, PublicProspect(
        full_name="Example Nora Lind",
        title="CTO",
        company_name="Example North",
        domain="example-north.test",
        industry="Software",
        country_code="SE",
        region="CA",
        city="Stockholm",
        employee_count=120,
        company_type="private",
        profile_url="https://www.linkedin.com/in/example-nora-lind",
        email="nora@example-north.test",
        email_status="inferred",
        technologies=[],
    ))
    synonym = _save(db, PublicProspect(
        full_name="Example Bea Holm",
        title="Chief Technology Officer",
        company_name="Example Holm",
        domain="example-holm.test",
        industry="Software",
        country_code="US",
        region="CA",
        city=None,
        employee_count=None,
        company_type=None,
        profile_url="https://www.linkedin.com/in/example-bea-holm",
        technologies=["AWS"],
    ))
    other_tech = _save(db, PublicProspect(
        full_name="Example Otto Berg",
        title="CTO",
        company_name="Example Berg",
        domain="example-berg.test",
        industry="Software",
        country_code="SE",
        city="Stockholm",
        employee_count=80,
        company_type="private",
        profile_url="https://www.linkedin.com/in/example-otto-berg",
        technologies=["Docker"],
    ))
    excluded_country = _save(db, PublicProspect(
        full_name="Example Dana Weiss",
        title="CTO",
        company_name="Example Weiss",
        domain="example-weiss.test",
        industry="Software",
        country_code="DE",
        city="Berlin",
        employee_count=90,
        company_type="private",
        profile_url="https://www.linkedin.com/in/example-dana-weiss",
        technologies=["AWS"],
    ))
    excluded_title = _save(db, PublicProspect(
        full_name="Example Ian Berg",
        title="intern",
        company_name="Example Ian",
        domain="example-ian.test",
        industry="Software",
        country_code="SE",
        city="Stockholm",
        employee_count=90,
        company_type="private",
        profile_url="https://www.linkedin.com/in/example-ian-berg",
        technologies=["AWS"],
    ))
    free_text = _save(db, PublicProspect(
        full_name="Example Vera Lind",
        title="Director of something cto adjacent",
        company_name="Example Vera",
        domain="example-vera.test",
        industry="Software",
        country_code="SE",
        city="Stockholm",
        employee_count=90,
        company_type="private",
        profile_url="https://www.linkedin.com/in/example-vera-lind",
        technologies=["AWS"],
    ))
    db.add(GlobalEvidence(
        subject_type="person",
        subject_id=kept.id,
        field_name="title",
        value_text="CTO",
        share_scope="public",
        eligible=True,
    ))
    db.commit()
    before = db.query(GlobalPerson).count()
    with _client(db) as client:
        headers = _admin(client, db, "match-admin@example.com")
        created = client.post(
            "/api/platform/discovery/targets",
            json={"name": "Example match", "schedule_type": "daily", "timezone": "UTC", "criteria": _criteria()},
            headers=headers,
        )
        assert created.status_code == 200, created.text
        preview = client.post(
            f"/api/platform/discovery/targets/{created.json()['id']}/preview",
            headers=headers,
        )
        assert preview.status_code == 200, preview.text
    target = db.get(DiscoveryTarget, created.json()["id"])
    matched_ids = {
        row.id
        for row in apply_criteria(
            db,
            db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None)),
            target.criteria,
            field_rules(db),
        )
    }
    assert kept.id in matched_ids
    assert synonym.id in matched_ids
    assert other_tech.id not in matched_ids
    assert excluded_country.id not in matched_ids
    assert excluded_title.id not in matched_ids
    assert free_text.id not in matched_ids
    assert preview.json()["eligible"] == len(matched_ids)
    assert preview.json()["enrichment"] == "unavailable"
    assert db.query(GlobalPerson).count() == before
    assert db.get(GlobalPerson, kept.id).email_status == "inferred"
    assert db.get(GlobalPerson, kept.id).quality_tier not in {"verified", "contactable"}
    assert db.query(GlobalEvidence).filter(GlobalEvidence.subject_id == kept.id, GlobalEvidence.field_name == "title").count() >= 1
    customer_total, _rows = match_people(db, CacheQuery(country_codes=["SE"], titles=["cto"]))
    assert customer_total >= 1


def test_required_field_changes_eligibility_without_deleting(db):
    seed_defaults(db)
    db.commit()
    present = _save(db, PublicProspect(
        full_name="Example Rita Holm",
        title="CTO",
        company_name="Example Rita",
        domain="example-rita.test",
        industry="Software",
        country_code="SE",
        profile_url="https://www.linkedin.com/in/example-rita-holm",
        email_status="inferred",
        email="rita@example-rita.test",
    ))
    missing = _save(db, PublicProspect(
        full_name="Example Nils Holm",
        title=None,
        company_name="Example Nils",
        domain="example-nils.test",
        industry=None,
        country_code="SE",
        profile_url="https://www.linkedin.com/in/example-nils-holm",
    ))
    db.commit()
    rule = db.query(DiscoveryFieldRule).filter(DiscoveryFieldRule.field_name == "title").one()
    rule.requirement = "required"
    rule.collection_enabled = True
    db.commit()
    target = DiscoveryTarget(
        name="Example required title",
        scheduling_status="paused",
        criteria={
            "geography": {"include_countries": ["SE"], "exclude_countries": []},
            "titles": {"exact": ["cto"], "synonyms": [], "exclude": []},
            "technologies": {"mode": "optional", "include": []},
        },
    )
    db.add(target)
    db.commit()
    ids = {
        row.id
        for row in apply_criteria(
            db,
            db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None)),
            target.criteria,
            field_rules(db),
        )
    }
    assert present.id in ids
    assert missing.id not in ids
    assert db.get(GlobalPerson, missing.id) is not None
    rule.collection_enabled = False
    db.commit()
    ids_off = {
        row.id
        for row in apply_criteria(
            db,
            db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None)),
            target.criteria,
            field_rules(db),
        )
    }
    assert missing.id in ids_off
    assert db.get(GlobalPerson, missing.id) is not None
    add_suppression(db, kind="email", value="rita@example-rita.test", reason="example", user_id=None)
    db.commit()
    from backend.services.discovery_engine.suppression import suppressed_people_filter

    visible = {
        row.id
        for row in apply_criteria(
            db,
            suppressed_people_filter(db, db.query(GlobalPerson).filter(GlobalPerson.archived_at.is_(None))),
            target.criteria,
            field_rules(db),
        )
    }
    assert present.id not in visible
    assert db.get(GlobalPerson, present.id) is not None


def test_invalid_schedule_bounds_and_contradictions_are_rejected(db):
    seed_defaults(db)
    db.commit()
    with _client(db) as client:
        headers = _admin(client, db, "bounds-admin@example.com")
        member = _auth(client, "bounds-member@example.com")
        owner = _auth(client, "bounds-owner@example.com", organization=True)
        for headers_denied in (member, owner):
            assert client.get("/api/platform/discovery/targets", headers=headers_denied).status_code == 403
            assert client.get("/api/platform/discovery/fields", headers=headers_denied).status_code == 403
            assert client.post("/api/platform/discovery/catalogue", json={"kind": "technology", "name": "Redis"}, headers=headers_denied).status_code == 403
        bad = [
            {"name": "Bad zone", "timezone": "Not/AZone", "criteria": {}},
            {"name": "Bad interval", "schedule_type": "interval", "interval_minutes": 5, "criteria": {}},
            {"name": "Bad hour", "hour": 25, "criteria": {}},
            {"name": "Bad batch", "max_people_per_run": 0, "criteria": {}},
            {"name": "Bad country", "criteria": {"geography": {"include_countries": ["ZZ"]}}},
            {"name": "Both countries", "criteria": {"geography": {"include_countries": ["SE"], "exclude_countries": ["SE"]}}},
            {"name": "Both titles", "criteria": {"titles": {"exact": ["CTO"], "exclude": ["CTO"]}}},
            {"name": "Bad employees", "criteria": {"company": {"employee_min": 100, "employee_max": 10}}},
            {"name": "Unknown tech", "criteria": {"technologies": {"mode": "optional", "include": ["Redis"]}}},
        ]
        for body in bad:
            response = client.post("/api/platform/discovery/targets", json=body, headers=headers)
            assert response.status_code == 400, body
        saved = client.patch(
            "/api/platform/discovery/fields/technology",
            json={"collection_enabled": True, "requirement": "optional", "priority": 80, "refresh_hours": 168, "verification_required": True},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["enrichment_source_status"] == "unavailable"
        listed = client.get("/api/platform/discovery/fields", headers=headers).json()
        tech = next(item for item in listed["items"] if item["field_name"] == "technology")
        assert tech["enrichment_source_status"] == "unavailable"
        assert listed["enrichment_application"] == "unavailable"


def test_validate_stage_classifies_without_calling_a_provider(db, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("provider called")

    monkeypatch.setattr("backend.services.discovery.runner.search_text", _boom)
    monkeypatch.setattr("backend.services.EmailModule.finder.find_email", _boom)
    seed_defaults(db)
    db.commit()
    _save(db, PublicProspect(
        full_name="Example Ada Found",
        title="CTO",
        company_name="Example Ada",
        domain="example-ada-target.test",
        industry="Software",
        country_code="SE",
        profile_url="https://www.linkedin.com/in/example-ada-found",
        technologies=[],
    ))
    target = db.query(DiscoveryTarget).filter(DiscoveryTarget.name == "US and Europe technology leaders").one()
    run = DiscoveryRun(
        target_id=target.id,
        idempotency_key="phase4-validate",
        status="queued",
        scheduled_for=__import__("datetime").datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    before = db.query(GlobalPerson).count()
    execute_run(db, run, attempt=1)
    db.refresh(run)
    assert run.status == "succeeded"
    detail = (
        db.query(DiscoveryStageAttempt)
        .filter(DiscoveryStageAttempt.run_id == run.id, DiscoveryStageAttempt.stage == "validate")
        .one()
    )
    assert detail.status == "classified"
    assert "were not deleted" in detail.detail
    assert "Enrichment sources are not applied" in detail.detail
    assert db.query(GlobalPerson).count() == before
    assert db.query(GlobalPersonTechnology).count() == 0
