"""Mark 2.0 comprehensive test suite.

Covers workspace management, RBAC, cross-tenant isolation, 1k/5k/10k target
sizing, multi-dimensional evidence scores, lead management lifecycle, bulk
operations, notes/tasks/activity logs, selective CRM transfer (no automatic
CRM pollution), nested groups/subgroups, smart groups, export history, and
dashboard/platform endpoints.
"""

from __future__ import annotations

import uuid
from fastapi.testclient import TestClient

from backend.database import SessionLocal, init_db
from backend.main import app
from backend.models import CrmContact, LeadRecord, SearchCandidate, SearchJob, SearchJobStatus
from backend.services.discovery.promote import promote_qualified
from backend.services.discovery.qualify import qualify_candidates
from backend.services.discovery.runner import candidate_pool_target

init_db()
client = TestClient(app)


def _register(prefix: str = "u"):
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "Password123!", "full_name": f"User {prefix}"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, r.json()["user"]


def _seed_leads(user_id: int, count: int = 5, org_id: int | None = None) -> tuple[int, list[int]]:
    db = SessionLocal()
    job = SearchJob(
        name="Solar Marketing Managers",
        prompt="Marketing Manager solar companies California",
        filters={"job_titles": ["Marketing Manager"], "industry": "solar", "location": "California"},
        owner_id=user_id,
        organization_id=org_id,
        status=SearchJobStatus.COMPLETED,
    )
    db.add(job)
    db.flush()
    for i in range(count):
        db.add(
            SearchCandidate(
                search_job_id=job.id,
                identity_key=f"id-{job.id}-{i}",
                name=f"Prospect {i}Person",
                title="Marketing Manager",
                company=f"SunSolar {i} Inc",
                location="Los Angeles, California",
                linkedin_url=f"https://www.linkedin.com/in/prospect-{job.id}-{i}",
                linkedin_url_normalized=f"https://www.linkedin.com/in/prospect-{job.id}-{i}",
                raw_snippet=(
                    f"Marketing Manager at SunSolar {i} Inc in California. "
                    + (f"Reach at prospect{i}@sunsolar.com" if i % 2 == 0 else "")
                ),
            )
        )
    db.commit()
    qualify_candidates(db, search_job_id=job.id, prompt=job.prompt, filters=job.filters)
    promote_qualified(db, job)
    lead_ids = [r.id for r in db.query(LeadRecord).filter(LeadRecord.search_job_id == job.id).order_by(LeadRecord.id).all()]
    job_id = job.id
    db.close()
    return job_id, lead_ids


def test_target_sizing_1000_5000_10000():
    """Requirements 6 & 7: 1,000 / 5,000 / 10,000 target lead count support."""
    assert candidate_pool_target({"max_results": 1000}) >= 5000
    assert candidate_pool_target({"max_results": 5000}) >= 10000
    f = client.get("/api/filters").json()["filters"]["max_results"]
    assert f["max"] == 10000
    assert 5000 in f["presets"] and 10000 in f["presets"]


def test_discovery_does_not_auto_populate_crm_and_computes_evidence_scores():
    """Discovered leads stay in lead_records with multi-score breakdown, NOT in CRM."""
    headers, user = _register("disc")
    _, lead_ids = _seed_leads(user["id"], count=4)
    assert len(lead_ids) == 4

    people = client.get("/api/people", headers=headers).json()
    assert people["page"]["total"] == 4
    first = people["items"][0]
    assert first["score_breakdown"] is not None
    assert set(first["score_breakdown"].keys()) >= {
        "person_relevance",
        "company_relevance",
        "job_title_match",
        "location_match",
        "identity_confidence",
    }

    crm = client.get("/api/crm/contacts", headers=headers).json()
    assert crm["page"]["total"] == 0


def test_workspaces_invitations_rbac_and_cross_tenant_isolation():
    """Requirements 2, 3, 4, 5: Workspaces, invites, roles, and strict tenant isolation."""
    h_owner, owner = _register("owner")
    h_viewer, _ = _register("viewer")
    h_stranger, _ = _register("stranger")

    ws = client.post("/api/organizations", json={"name": f"SolarWS-{uuid.uuid4().hex[:6]}"}, headers=h_owner)
    assert ws.status_code == 200
    org_id = ws.json()["id"]

    # Rename workspace
    rn = client.patch(f"/api/organizations/{org_id}", json={"name": f"Renamed-{uuid.uuid4().hex[:6]}"}, headers=h_owner)
    assert rn.status_code == 200

    # Stranger cannot view members or leads
    assert client.get(f"/api/organizations/{org_id}/members", headers=h_stranger).status_code == 403

    # Invite viewer
    viewer_email = client.get("/api/auth/me", headers=h_viewer).json()["email"]
    inv = client.post(
        f"/api/organizations/{org_id}/members",
        json={"email": viewer_email, "role": "viewer"},
        headers=h_owner,
    )
    assert inv.status_code == 200

    # Viewer has read access, but cannot create CRM groups in the workspace
    assert client.get(f"/api/organizations/{org_id}/members", headers=h_viewer).status_code == 200
    deny = client.post(
        "/api/crm/groups",
        json={"name": "Forbidden", "organization_id": org_id},
        headers=h_viewer,
    )
    assert deny.status_code == 403

    # Cross-workspace lead isolation
    _, owner_leads = _seed_leads(owner["id"], count=2)
    assert client.get(f"/api/people/{owner_leads[0]}", headers=h_stranger).status_code == 404
    assert client.patch(f"/api/people/{owner_leads[0]}", json={"lead_status": "qualified"}, headers=h_stranger).status_code == 404


def test_lead_lifecycle_notes_tasks_activity_and_bulk_operations():
    """Requirements 14: Lead management lifecycle, notes, tasks, activity, bulk updates."""
    headers, user = _register("mgr")
    _, lead_ids = _seed_leads(user["id"], count=6)
    lid = lead_ids[0]

    # Status + priority update
    up = client.patch(
        f"/api/people/{lid}",
        json={"lead_status": "interested", "priority": "high", "tags": ["solar", "vip"]},
        headers=headers,
    )
    assert up.status_code == 200
    assert up.json()["lead_status"] == "interested"

    # Note & task
    n = client.post(f"/api/people/{lid}/notes", json={"body": "Spoke with VP"}, headers=headers)
    assert n.status_code == 200
    t = client.post(f"/api/people/{lid}/tasks", json={"title": "Send proposal"}, headers=headers)
    assert t.status_code == 200
    tid = t.json()["id"]
    tc = client.patch(f"/api/people/{lid}/tasks/{tid}", json={"completed": True}, headers=headers)
    assert tc.json()["completed"] is True

    detail = client.get(f"/api/people/{lid}", headers=headers).json()
    assert len(detail["notes"]) == 1
    assert len(detail["tasks"]) == 1
    assert len(detail["activities"]) >= 3

    # Bulk update & archive
    b = client.post(
        "/api/people/bulk",
        json={"lead_ids": lead_ids[1:4], "action": "update", "lead_status": "qualified"},
        headers=headers,
    )
    assert b.json()["affected"] == 3

    arch = client.post(
        "/api/people/bulk",
        json={"lead_ids": [lead_ids[4]], "action": "archive"},
        headers=headers,
    )
    assert arch.json()["affected"] == 1
    # Archived lead excluded from default view, restored on restore
    active_list = client.get("/api/people", headers=headers).json()
    assert active_list["page"]["total"] == 5
    client.post("/api/people/bulk", json={"lead_ids": [lead_ids[4]], "action": "restore"}, headers=headers)
    assert client.get("/api/people", headers=headers).json()["page"]["total"] == 6


def test_crm_groups_nested_subgroups_smart_groups_and_selective_transfer():
    """Requirements 15, 16, 17, 18, 19, 20: Nested CRM groups, smart groups, selective transfer & dedup."""
    headers, user = _register("crm")
    _, lead_ids = _seed_leads(user["id"], count=5)

    # Create parent group + nested subgroup + smart group
    parent = client.post("/api/crm/groups", json={"name": "Solar Companies"}, headers=headers).json()
    sub_ca = client.post(
        "/api/crm/groups",
        json={"name": "California", "parent_id": parent["id"]},
        headers=headers,
    ).json()
    sub_tx = client.post(
        "/api/crm/groups",
        json={"name": "Texas", "parent_id": parent["id"]},
        headers=headers,
    ).json()
    smart = client.post(
        "/api/crm/groups",
        json={"name": "CA Managers", "kind": "smart", "smart_rules": {"location": "California", "job_title": "Manager"}},
        headers=headers,
    ).json()

    # Preview transfer of 3 selected leads (out of 5 discovered)
    prev = client.post(
        "/api/crm/transfer/preview",
        json={"lead_ids": lead_ids[:3], "group_id": parent["id"], "subgroup_id": sub_ca["id"]},
        headers=headers,
    ).json()
    assert prev["selected_count"] == 3
    assert prev["with_email"] >= 1
    assert prev["without_email"] >= 1

    # Execute selective transfer — only 3 enter CRM; 2 stay only in discovery
    tx = client.post(
        "/api/crm/transfer",
        json={"lead_ids": lead_ids[:3], "group_id": parent["id"], "subgroup_id": sub_ca["id"]},
        headers=headers,
    ).json()
    assert tx["transferred_count"] == 3

    crm_all = client.get("/api/crm/contacts", headers=headers).json()
    assert crm_all["page"]["total"] == 3

    # Re-transferring the same leads detects duplicates and does not create extra CRM rows
    tx_dup = client.post(
        "/api/crm/transfer",
        json={"lead_ids": lead_ids[:3], "group_id": parent["id"], "subgroup_id": sub_ca["id"]},
        headers=headers,
    ).json()
    assert tx_dup["transferred_count"] == 0
    assert tx_dup["duplicate_count"] == 3
    assert client.get("/api/crm/contacts", headers=headers).json()["page"]["total"] == 3

    # Parent group query includes nested subgroup contacts
    in_parent = client.get(f"/api/crm/contacts?group_id={parent['id']}", headers=headers).json()
    assert in_parent["page"]["total"] == 3

    # Smart group dynamically matches contacts
    in_smart = client.get(f"/api/crm/contacts?group_id={smart['id']}", headers=headers).json()
    assert in_smart["page"]["total"] == 3

    # Move 1 contact from California subgroup to Texas subgroup
    cid = crm_all["items"][0]["id"]
    mv = client.post(
        f"/api/crm/groups/{sub_ca['id']}/move",
        json={"contact_ids": [cid], "target_group_id": sub_tx["id"]},
        headers=headers,
    )
    assert mv.status_code == 200
    assert client.get(f"/api/crm/contacts?group_id={sub_tx['id']}", headers=headers).json()["page"]["total"] == 1

    # CSV export & export history
    csv_res = client.get(f"/api/crm/export?format=csv&group_id={parent['id']}", headers=headers)
    assert csv_res.status_code == 200
    assert "Prospect 0Person" in csv_res.text
    exports = client.get("/api/crm/exports", headers=headers).json()["items"]
    assert len(exports) >= 2


def test_dashboard_platform_jobs_presets_and_automations():
    """Requirements 17, 23, 24, 25: Real dashboard stats, presets, automations, search history."""
    headers, user = _register("plat")
    job_id, _ = _seed_leads(user["id"], count=3)

    dash = client.get("/api/platform/dashboard", headers=headers).json()
    assert dash["total_leads_discovered"] == 3
    assert dash["crm_contacts"] == 0

    jobs = client.get("/api/platform/jobs", headers=headers).json()
    assert any(j["id"] == job_id for j in jobs["search_jobs"])
    assert jobs["concurrency"]["search_max_concurrency"] >= 1

    preset = client.post(
        "/api/platform/presets",
        json={"name": "Solar Verified", "scope": "leads", "filters": {"email_status": "verified"}},
        headers=headers,
    )
    assert preset.status_code == 200
    assert len(client.get("/api/platform/presets", headers=headers).json()["items"]) == 1

    # Silent external CRM sync in automations is rejected
    bad_auto = client.post(
        "/api/platform/automations",
        json={"name": "Bad", "trigger": "email_verified", "actions": {"sync_external_crm": True}},
        headers=headers,
    )
    assert bad_auto.status_code == 400

    good_auto = client.post(
        "/api/platform/automations",
        json={"name": "Qualify verified", "trigger": "email_verified", "actions": {"set_status": "qualified"}},
        headers=headers,
    )
    assert good_auto.status_code == 200

    # Rename search
    rn = client.patch(f"/api/searches/{job_id}", json={"name": "Solar CA Updated"}, headers=headers)
    assert rn.status_code == 200
    assert rn.json()["name"] == "Solar CA Updated"
