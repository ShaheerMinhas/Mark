"""Run a few real searches and print only the discovery funnel.

Stops when leads are visible (enrichment is a separate, slower pass). Use this
to compare prompt shapes without waiting ten minutes for public-web emails.

    .venv/bin/python -u scripts/live_cases.py
"""

from __future__ import annotations

import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000"

CASES = [
    {
        "name": "Swedish logistics marketers",
        "prompt": "Marketing managers in Swedish logistics companies",
        "filters": {
            "job_titles": ["Marketing Manager", "Head of Marketing"],
            "location": "Sweden",
            "industry": "logistics",
            "active_only": False,
        },
    },
    {
        "name": "Dutch SaaS CTOs",
        "prompt": "CTOs at SaaS companies in the Netherlands",
        "filters": {
            "job_titles": ["CTO", "Chief Technology Officer"],
            "location": "Netherlands",
            "industry": "software",
            "active_only": False,
        },
    },
    {
        "name": "German manufacturing HR",
        "prompt": "HR directors in German manufacturing companies",
        "filters": {
            "job_titles": ["HR Director", "Head of HR"],
            "location": "Germany",
            "industry": "manufacturing",
            "active_only": False,
        },
    },
]


def run_case(client: httpx.Client, case: dict, timeout: float = 240.0) -> dict:
    t0 = time.monotonic()
    created = client.post(
        "/api/searches",
        json={
            "name": case["name"],
            "prompt": case["prompt"],
            "filters": case["filters"],
            "save_results": True,
        },
    )
    created.raise_for_status()
    job_id = created.json()["search"]["id"]
    print(f"\n=== {case['name']}  job {job_id} ===")
    last = ""
    while time.monotonic() - t0 < timeout:
        status = client.get(f"/api/search-jobs/{job_id}").json()
        line = (
            f"  [{time.monotonic() - t0:5.0f}s] {status['status']:<22} "
            f"q={status.get('completed_queries', 0)}/{status.get('total_queries', 0)} "
            f"cand={status.get('candidates_found', 0)} leads={status.get('lead_count', 0)}"
        )
        if line != last:
            print(line)
            last = line
        if status["status"] in {"enriching", "completed", "partially_completed", "failed", "cancelled"}:
            if status["status"] != "enriching" or status.get("lead_count", 0) > 0:
                break
        time.sleep(2)

    funnel = client.get(f"/api/search-jobs/{job_id}/funnel").json()
    page = client.get(f"/api/search-jobs/{job_id}/leads?page=1&page_size=5").json()
    elapsed = time.monotonic() - t0
    relevant = funnel.get("relevant_leads") or 0
    linkedin = funnel.get("linkedin_leads") or 0
    print(
        f"  -> {elapsed:.0f}s  candidates={funnel.get('candidates_found')}  "
        f"leads={relevant}  linkedin={linkedin}  "
        f"emails={funnel.get('contactable_leads')}  "
        f"rejections={funnel.get('rejections')}  "
        f"queries={funnel.get('query_outcomes')}"
    )
    for lead in page.get("items") or []:
        print(
            f"     {lead['relevance_score']:.2f}  {lead['name']:<22} "
            f"{(lead.get('work_designation') or '-')[:28]:<28} "
            f"{(lead.get('company_name') or '-')[:24]}"
        )
    return {"name": case["name"], "job_id": job_id, "elapsed": elapsed, **funnel}


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30.0)
    email = f"cases-{uuid.uuid4().hex[:8]}@example.com"
    reg = client.post(
        "/api/auth/register",
        json={"email": email, "password": "SmokeTest123!", "full_name": "Case Runner"},
    )
    if reg.status_code >= 400:
        print("register failed:", reg.status_code, reg.text[:300])
        return 1
    client.headers["Authorization"] = f"Bearer {reg.json()['access_token']}"
    print(f"registered {email}")

    rows = [run_case(client, case) for case in CASES]
    print("\n=== summary ===")
    for row in rows:
        print(
            f"  {row['name']:<32} leads={row.get('relevant_leads', 0):>4}  "
            f"cand={row.get('candidates_found', 0):>4}  "
            f"li={row.get('linkedin_leads', 0):>4}  "
            f"{row['elapsed']:.0f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
