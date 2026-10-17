"""Live end-to-end smoke test against a running dev server.

Creates a real search and polls the job endpoint, printing the progressive state
so the staged pipeline can be observed rather than inferred. Not part of the
automated suite: this one does hit the real internet.

    .venv/bin/python scripts/live_smoke.py
"""

from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000"
PROMPT = "Marketing managers in Swedish logistics companies"
FILTERS = {
    "job_titles": ["Marketing Manager", "Head of Marketing"],
    "location": "Sweden",
    "industry": "logistics",
    "active_only": False,
    "max_results": 1000,
}


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30.0)

    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post(
        "/api/auth/register",
        json={"email": email, "password": "SmokeTest123!", "full_name": "Smoke Test"},
    )
    if reg.status_code >= 400:
        print("register failed:", reg.status_code, reg.text[:300])
        return 1
    token = reg.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    print(f"registered {email}")

    t0 = time.monotonic()
    created = client.post(
        "/api/searches",
        json={"name": "Live smoke", "prompt": PROMPT, "filters": FILTERS},
    )
    handoff = time.monotonic() - t0
    if created.status_code >= 400:
        print("create failed:", created.status_code, created.text[:300])
        return 1
    job = created.json()["search"]
    job_id = job["id"]
    print(f"POST /api/searches returned in {handoff:.2f}s -> job {job_id} ({job['status']})")
    if handoff > 5:
        print("!! the create call blocked; it should return immediately")

    first_lead_at = None
    last = ""
    # Stay until email lookup has actually finished, not just until discovery
    # marks the search complete. Emails are the deliverable.
    deadline = time.monotonic() + float(sys.argv[1]) if len(sys.argv) > 1 else time.monotonic() + 600
    while time.monotonic() < deadline:
        status = client.get(f"/api/search-jobs/{job_id}").json()
        line = (
            f"[{time.monotonic() - t0:6.1f}s] {status['status']:<20} "
            f"{status['progress']:>3}% "
            f"q={status.get('completed_queries', 0)}/{status.get('total_queries', 0)} "
            f"cand={status.get('candidates_found', 0)} "
            f"leads={status.get('lead_count', 0)} "
            f"emails={status.get('emails_found', 0)} "
            f"enr={status.get('enrichment_completed', 0)}"
        )
        if line[9:] != last:
            print(line)
            last = line[9:]
        if first_lead_at is None and status.get("lead_count", 0) > 0:
            first_lead_at = time.monotonic() - t0
            print(f"    ** first leads visible at {first_lead_at:.1f}s")
        state = status["status"]
        leads = int(status.get("lead_count") or 0)
        enriched = int(status.get("enrichment_completed") or 0)
        if state in {"failed", "cancelled"}:
            break
        if state in {"completed", "partially_completed"} and (leads == 0 or enriched >= leads):
            break
        time.sleep(2)

    total = time.monotonic() - t0
    funnel = client.get(f"/api/search-jobs/{job_id}/funnel").json()
    page = client.get(f"/api/search-jobs/{job_id}/leads?page=1&page_size=10").json()

    print("\n--- funnel ---")
    for key, value in funnel.items():
        if not isinstance(value, (dict, list)):
            print(f"  {key}: {value}")
    print(f"  rejections: {funnel.get('rejections')}")
    print(f"  query outcomes: {funnel.get('query_outcomes')}")
    relevant = funnel.get("relevant_leads") or 0
    contactable = funnel.get("contactable_leads") or 0
    linkedin = funnel.get("linkedin_leads") or 0
    if relevant:
        print(
            f"  email coverage: {contactable}/{relevant} = {contactable / relevant * 100:.0f}%"
        )
        print(f"  linkedin-only: {max(0, linkedin - contactable)}")

    print("\n--- first page ---")
    meta = page["page"]
    print(f"  page {meta['page']} of {meta['total_pages']}, {meta['total']} leads total")
    for lead in page["items"][:10]:
        print(
            f"  {lead['relevance_score']:.2f}  {lead['name']:<24} "
            f"{(lead.get('work_designation') or '-')[:30]:<30} "
            f"{(lead.get('company_name') or '-')[:26]:<26} {lead.get('email') or ''}"
        )

    print(f"\ntotal wall time: {total:.1f}s")
    print(f"first leads visible after: {first_lead_at}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
