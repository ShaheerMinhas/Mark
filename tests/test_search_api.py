"""HTTP surface: job creation, queueing, polling, pagination, and the legacy API.

Phase 15 requires the pre-existing endpoints to keep working, so they are asserted
here alongside the new ones.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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
            "email": "api-user@example.com",
            "password": "test-password-123",
            "full_name": "API User",
        },
    )
    login = client.post(
        "/api/auth/login",
        json={"email": "api-user@example.com", "password": "test-password-123"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def create_search(client, auth, **overrides):
    body = {
        "name": "Swedish logistics marketing",
        "prompt": "Marketing managers in Swedish logistics companies",
        "filters": {
            "job_titles": ["Marketing Manager"],
            "location": "Sweden",
            "industry": "logistics",
        },
        "save_results": True,
        **overrides,
    }
    response = client.post("/api/searches", json=body, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


# --- Creation and queueing ------------------------------------------------


def test_creating_a_search_returns_immediately_with_a_job_id(client, auth, db):
    payload = create_search(client, auth)

    assert payload["search"]["id"] > 0
    assert payload["leads"] == []
    assert payload["count"] == 0
    assert payload["search"]["status"] == SearchJobStatus.PENDING.value


def test_creating_a_search_queues_discovery_work(client, auth, db):
    payload = create_search(client, auth)

    queued = db.query(QueuedJob).filter(
        QueuedJob.job_type == JobType.SEARCH_DISCOVERY.value
    ).one()
    assert queued.status == QueuedJobStatus.QUEUED.value
    assert queued.payload["search_job_id"] == payload["search"]["id"]
    assert queued.trace_id


def test_a_new_search_job_records_a_trace_id(client, auth, db):
    payload = create_search(client, auth)

    job = db.get(SearchJob, payload["search"]["id"])
    assert job.trace_id
    assert job.status == SearchJobStatus.PENDING


def test_creating_a_search_does_not_run_discovery_inline(client, auth, db):
    """The HTTP request must not be held open for the pipeline."""
    payload = create_search(client, auth)

    job = db.get(SearchJob, payload["search"]["id"])
    assert job.started_at is None
    assert db.query(SearchCandidate).count() == 0


def test_invalid_age_range_is_still_rejected(client, auth):
    response = client.post(
        "/api/searches",
        json={
            "name": "Bad range",
            "prompt": "Marketing managers",
            "filters": {"age_min": 50, "age_max": 30},
        },
        headers=auth,
    )

    assert response.status_code == 400


def test_search_endpoints_require_authentication(client):
    assert client.post("/api/searches", json={"name": "x", "prompt": "y"}).status_code == 401
    assert client.get("/api/search-jobs/1").status_code == 401


# --- Progress polling -----------------------------------------------------


def test_progress_endpoint_exposes_stage_and_counters(client, auth, db):
    payload = create_search(client, auth)
    search_id = payload["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.DISCOVERING
    job.current_stage = "discovery"
    job.total_queries = 10
    job.completed_queries = 4
    job.candidates_found = 67
    job.candidates_deduplicated = 12
    job.progress = 32
    db.commit()

    body = client.get(f"/api/search-jobs/{search_id}", headers=auth).json()

    assert body["status"] == "discovering"
    assert body["legacy_status"] == "running"
    assert body["current_stage"] == "discovery"
    assert body["total_queries"] == 10
    assert body["completed_queries"] == 4
    assert body["candidates_found"] == 67
    assert body["candidates_deduplicated"] == 12
    assert body["progress"] == 32


def test_progress_hides_internal_error_detail(client, auth, db):
    payload = create_search(client, auth)
    search_id = payload["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.FAILED
    job.message = "Search could not be completed. Please try again."
    job.error_message = "all_queries_failed"
    db.commit()

    body = client.get(f"/api/search-jobs/{search_id}", headers=auth).json()

    assert "Traceback" not in body["message"]
    assert body["error_message"] == "all_queries_failed"


def test_another_users_search_is_not_visible(client, auth, db, user):
    foreign = SearchJob(
        name="Someone else", prompt="x", filters={}, owner_id=user.id,
        status=SearchJobStatus.COMPLETED,
    )
    db.add(foreign)
    db.commit()

    assert client.get(f"/api/search-jobs/{foreign.id}", headers=auth).status_code == 404


def test_unknown_search_returns_404(client, auth):
    assert client.get("/api/search-jobs/999999", headers=auth).status_code == 404


# --- Pagination -----------------------------------------------------------


@pytest.fixture
def populated(client, auth, db):
    payload = create_search(client, auth)
    search_id = payload["search"]["id"]
    for index in range(120):
        db.add(
            LeadRecord(
                search_job_id=search_id,
                identity_key=f"li:person-{index}",
                name=f"Person Number{index:03d}",
                company_name="Nordic Freight AB" if index % 2 else "Baltic Logistics AB",
                work_designation="Marketing Manager",
                relevance_score=round(0.4 + index / 1000, 3),
                linkedin_url=f"https://www.linkedin.com/in/person-{index}",
                email=f"person{index}@example.com" if index % 3 == 0 else None,
                enrichment_status=(
                    EnrichmentStatus.ENRICHED.value
                    if index % 3 == 0
                    else EnrichmentStatus.PENDING.value
                ),
            )
        )
    db.commit()
    return search_id


def test_leads_are_returned_one_page_at_a_time(client, auth, populated):
    body = client.get(
        f"/api/search-jobs/{populated}/leads?page=1&page_size=25", headers=auth
    ).json()

    assert len(body["items"]) == 25
    assert body["page"] == {
        "page": 1,
        "page_size": 25,
        "total": 120,
        "total_pages": 5,
        "has_next": True,
    }


def test_the_last_page_reports_no_next_page(client, auth, populated):
    body = client.get(
        f"/api/search-jobs/{populated}/leads?page=5&page_size=25", headers=auth
    ).json()

    assert len(body["items"]) == 20
    assert body["page"]["has_next"] is False


def test_page_size_is_capped(client, auth, populated):
    assert (
        client.get(f"/api/search-jobs/{populated}/leads?page_size=5000", headers=auth).status_code
        == 422
    )


def test_leads_default_to_highest_relevance_first(client, auth, populated):
    items = client.get(f"/api/search-jobs/{populated}/leads", headers=auth).json()["items"]

    scores = [item["relevance_score"] for item in items]
    assert scores == sorted(scores, reverse=True)


def test_leads_can_be_sorted_by_name(client, auth, populated):
    items = client.get(
        f"/api/search-jobs/{populated}/leads?sort=name", headers=auth
    ).json()["items"]

    names = [item["name"] for item in items]
    assert names == sorted(names)


def test_leads_can_be_filtered_to_contactable_only(client, auth, populated):
    body = client.get(
        f"/api/search-jobs/{populated}/leads?has_email=true", headers=auth
    ).json()

    assert body["page"]["total"] == 40
    assert all(item["email"] for item in body["items"])


def test_the_default_lead_list_keeps_people_who_only_have_a_linkedin_url(
    client, auth, populated
):
    """Missing an email is not a reason to hide a relevant lead."""
    body = client.get(
        f"/api/search-jobs/{populated}/leads?page_size=50", headers=auth
    ).json()

    assert body["page"]["total"] == 120
    without_email = [item for item in body["items"] if not item.get("email")]
    assert without_email
    assert all(item["linkedin_url"] for item in without_email)


def test_leads_can_be_filtered_by_minimum_score(client, auth, populated):
    body = client.get(
        f"/api/search-jobs/{populated}/leads?min_score=0.5", headers=auth
    ).json()

    assert 0 < body["page"]["total"] < 120
    assert all(item["relevance_score"] >= 0.5 for item in body["items"])


def test_leads_can_be_text_searched(client, auth, populated):
    body = client.get(
        f"/api/search-jobs/{populated}/leads?search=Baltic", headers=auth
    ).json()

    assert body["page"]["total"] == 60


# --- Funnel: the four counts stay distinct --------------------------------


def test_funnel_separates_candidates_relevant_enriched_and_contactable(
    client, auth, populated, db
):
    for index in range(200):
        db.add(
            SearchCandidate(
                search_job_id=populated,
                identity_key=f"cand-{index}",
                name=f"Candidate Number{index:03d}",
                status="rejected" if index >= 120 else "qualified",
                rejection_reason="below_minimum_score" if index >= 120 else None,
            )
        )
    db.commit()

    body = client.get(f"/api/search-jobs/{populated}/funnel", headers=auth).json()

    assert body["candidates_found"] == 200
    assert body["relevant_leads"] == 120
    assert body["linkedin_leads"] == 120
    assert body["enriched_leads"] == 40
    assert body["contactable_leads"] == 40
    assert body["phoneable_leads"] == 0
    assert body["rejections"]["below_minimum_score"] == 80


def test_candidate_pool_exposes_rejection_reasons(client, auth, populated, db):
    db.add(
        SearchCandidate(
            search_job_id=populated,
            identity_key="cand-rejected",
            name="Per Nilsson",
            status="rejected",
            rejection_reason="title_mismatch",
        )
    )
    db.commit()

    body = client.get(
        f"/api/search-jobs/{populated}/candidates?status=rejected", headers=auth
    ).json()

    assert body["page"]["total"] == 1
    assert body["items"][0]["rejection_reason"] == "title_mismatch"


# --- Cancellation and enrichment retry ------------------------------------


def test_a_running_search_can_be_cancelled(client, auth, db):
    search_id = create_search(client, auth)["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.DISCOVERING
    db.commit()

    body = client.post(f"/api/search-jobs/{search_id}/cancel", headers=auth).json()

    db.refresh(job)
    assert body["cancel_requested"] is True
    assert job.cancel_requested is True


def test_a_finished_search_cannot_be_cancelled(client, auth, db):
    search_id = create_search(client, auth)["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.COMPLETED
    db.commit()

    assert client.post(f"/api/search-jobs/{search_id}/cancel", headers=auth).status_code == 409


def test_enrichment_can_be_retried_for_rate_limited_leads(client, auth, db):
    search_id = create_search(client, auth)["search"]["id"]
    db.add(
        LeadRecord(
            search_job_id=search_id,
            identity_key="li:anna-lind",
            name="Anna Lind",
            enrichment_status=EnrichmentStatus.RATE_LIMITED.value,
        )
    )
    db.commit()

    response = client.post(f"/api/search-jobs/{search_id}/enrich", headers=auth)

    assert response.status_code == 200
    lead = db.query(LeadRecord).filter(LeadRecord.search_job_id == search_id).one()
    assert lead.enrichment_status == EnrichmentStatus.PENDING.value
    assert (
        db.query(QueuedJob)
        .filter(QueuedJob.job_type == JobType.SEARCH_ENRICHMENT.value)
        .count()
        == 1
    )


def test_retrying_enrichment_with_nothing_pending_is_rejected(client, auth, db):
    search_id = create_search(client, auth)["search"]["id"]
    db.add(
        LeadRecord(
            search_job_id=search_id,
            identity_key="li:anna-lind",
            name="Anna Lind",
            enrichment_status=EnrichmentStatus.ENRICHED.value,
        )
    )
    db.commit()

    assert client.post(f"/api/search-jobs/{search_id}/enrich", headers=auth).status_code == 409


# --- Existing functionality must keep working (Phase 15) ------------------


def test_legacy_search_detail_endpoint_still_responds(client, auth, db):
    search_id = create_search(client, auth)["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.COMPLETED
    job.message = "2 relevant leads"
    db.commit()
    db.add(
        LeadRecord(
            search_job_id=search_id, identity_key="li:anna-lind", name="Anna Lind",
            email="anna@example.com",
        )
    )
    db.commit()

    body = client.get(f"/api/searches/{search_id}", headers=auth).json()

    assert body["count"] == 1
    assert body["leads"][0]["name"] == "Anna Lind"
    assert body["search"]["status"] == "completed"


def test_staged_statuses_are_reported_as_running_to_legacy_clients(client, auth, db):
    """The current frontend only understands the original four statuses."""
    search_id = create_search(client, auth)["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.ENRICHING
    db.commit()

    body = client.get(f"/api/searches/{search_id}", headers=auth).json()

    assert body["search"]["status"] == "running"
    assert body["search"]["detailed_status"] == "enriching"


def test_partially_completed_reads_as_completed_for_legacy_clients(client, auth, db):
    search_id = create_search(client, auth)["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.PARTIALLY_COMPLETED
    db.commit()

    body = client.get(f"/api/searches/{search_id}", headers=auth).json()

    assert body["search"]["status"] == "completed"
    assert body["search"]["detailed_status"] == "partially_completed"


def test_saved_searches_list_still_works(client, auth):
    create_search(client, auth)

    body = client.get("/api/searches", headers=auth).json()

    assert len(body) == 1
    assert body[0]["name"] == "Swedish logistics marketing"


def test_filter_definitions_endpoint_is_unchanged(client):
    body = client.get("/api/filters").json()

    assert "job_titles" in body["filters"]
    assert "location" in body["filters"]


@pytest.mark.parametrize("fmt,marker", [("csv", "name"), ("xml", "<leads>")])
def test_csv_and_xml_export_still_work(client, auth, db, fmt, marker):
    search_id = create_search(client, auth)["search"]["id"]
    job = db.get(SearchJob, search_id)
    job.status = SearchJobStatus.COMPLETED
    db.commit()
    db.add(
        LeadRecord(
            search_job_id=search_id, identity_key="li:anna-lind", name="Anna Lind",
            email="anna@example.com", company_name="Nordic Freight AB",
        )
    )
    db.commit()

    response = client.get(f"/api/searches/{search_id}/export?format={fmt}", headers=auth)

    assert response.status_code == 200
    assert marker in response.text
    assert "Anna Lind" in response.text


def test_health_endpoint_is_unchanged(client):
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["linkedin_login_required"] is False
