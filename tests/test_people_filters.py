"""Lead list filters: email presence is not the same thing as a lookup result."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.models import LeadRecord, SearchJob, SearchJobStatus


@pytest.fixture
def client(db):
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth(client):
    client.post(
        "/api/auth/register",
        json={
            "email": "people-user@example.com",
            "password": "test-password-123",
            "full_name": "People User",
        },
    )
    login = client.post(
        "/api/auth/login",
        json={"email": "people-user@example.com", "password": "test-password-123"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _seed(db, user_id: int) -> None:
    job = SearchJob(
        name="Example solar search",
        prompt="Marketing managers at solar companies",
        filters={"enrichment_mode": "after_discovery"},
        owner_id=user_id,
        status=SearchJobStatus.COMPLETED,
    )
    db.add(job)
    db.flush()
    looked_up = datetime(2026, 1, 2, 12, 0, 0)
    rows = [
        dict(name="Ada With", email="ada@example.com", email_status="found", email_enriched_at=looked_up, phone="555-0100", priority="high", company_name="Sun Co", work_designation="Director", relevance_score=0.9),
        dict(name="Bea Blank", email=None, email_status=None, email_enriched_at=None, phone=None, priority="low", company_name="Sun Co", work_designation="Manager", relevance_score=0.2),
        dict(name="Cy None", email=None, email_status="not_found", email_enriched_at=looked_up, phone="555-0102", priority="medium", company_name="Wind Co", work_designation="Analyst", relevance_score=0.5),
        dict(name="Dee Address", email="dee@example.com", email_status=None, email_enriched_at=None, phone=None, priority="medium", company_name="Wind Co", work_designation="Chief", relevance_score=0.4),
    ]
    for index, fields in enumerate(rows):
        db.add(LeadRecord(search_job_id=job.id, identity_key=f"ex-{index}", **fields))
    db.commit()


def test_email_filters_split_with_without_and_not_yet_searched(client, auth, db):
    from backend.models import User

    user = db.query(User).filter(User.email == "people-user@example.com").one()
    _seed(db, user.id)

    everyone = client.get("/api/people", headers=auth).json()
    assert everyone["page"]["total"] == 4
    assert everyone["email_counts"] == {
        "all": 4,
        "with_email": 2,
        "without_email": 2,
        "not_searched": 1,
        "no_email_found": 1,
    }
    assert everyone["library_total"] == 4

    with_email = client.get("/api/people?email_status=with_email&sort=name", headers=auth).json()
    assert [item["name"] for item in with_email["items"]] == ["Ada With", "Dee Address"]

    without = client.get("/api/people?email_status=without_email&sort=name", headers=auth).json()
    assert [item["name"] for item in without["items"]] == ["Bea Blank", "Cy None"]

    not_searched = client.get("/api/people?email_status=not_searched", headers=auth).json()
    assert [item["name"] for item in not_searched["items"]] == ["Bea Blank"]

    looked = client.get("/api/people?email_status=no_email_found", headers=auth).json()
    assert [item["name"] for item in looked["items"]] == ["Cy None"]

    # Counts stay on the unfiltered email split when another filter is applied with them.
    narrowed = client.get("/api/people?company=Wind&email_status=with_email", headers=auth).json()
    assert [item["name"] for item in narrowed["items"]] == ["Dee Address"]
    assert narrowed["email_counts"]["all"] == 2
    assert narrowed["email_counts"]["with_email"] == 1
    assert narrowed["email_counts"]["no_email_found"] == 1


def test_filters_compose_and_sorts_use_stored_fields(client, auth, db):
    from backend.models import User

    user = db.query(User).filter(User.email == "people-user@example.com").one()
    _seed(db, user.id)

    high_with_phone = client.get(
        "/api/people?priority=high&has_phone=yes",
        headers=auth,
    ).json()
    assert [item["name"] for item in high_with_phone["items"]] == ["Ada With"]

    no_phone = client.get("/api/people?has_phone=no&sort=name", headers=auth).json()
    assert [item["name"] for item in no_phone["items"]] == ["Bea Blank", "Dee Address"]

    by_email = client.get("/api/people?sort=email", headers=auth).json()
    assert [item["name"] for item in by_email["items"][:2]] == ["Ada With", "Dee Address"]

    by_priority = client.get("/api/people?sort=priority", headers=auth).json()
    assert by_priority["items"][0]["name"] == "Ada With"
    assert by_priority["items"][-1]["name"] == "Bea Blank"

    by_title = client.get("/api/people?sort=title", headers=auth).json()
    assert [item["work_designation"] for item in by_title["items"]] == [
        "Analyst",
        "Chief",
        "Director",
        "Manager",
    ]

    empty = client.get("/api/people?company=Missing%20Co&priority=high", headers=auth).json()
    assert empty["page"]["total"] == 0
    assert empty["library_total"] == 4
    assert empty["email_counts"]["all"] == 0
