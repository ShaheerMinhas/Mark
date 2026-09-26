#!/usr/bin/env python3
"""Look up a real public email for a person (or every lead in a saved search).

Examples:
  .venv/bin/python scripts/fetch_emails.py "Sajjad Ahmed" --company "Xavor Corporation"
  .venv/bin/python scripts/fetch_emails.py --name "Jane Doe" --company Acme --domain acme.com --all
  .venv/bin/python scripts/fetch_emails.py --search-id 13 --save

Does not invent addresses. Optional: set HUNTER_API_KEY for Hunter.io.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.EmailModule import find_email, find_emails  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Find a real public email for a person")
    p.add_argument("name", nargs="?", default="", help="Full name")
    p.add_argument("--name", dest="name_opt", default="", help="Full name (alternative to positional)")
    p.add_argument("--company", default="", help="Employer name")
    p.add_argument("--domain", default="", help="Company website host, e.g. xavor.com")
    p.add_argument("--snippet", default="", help="Known profile text to scan first")
    p.add_argument("--search-id", type=int, default=None, help="Look up emails for leads in this saved search")
    p.add_argument("--limit", type=int, default=25, help="Max leads when using --search-id")
    p.add_argument("--all", action="store_true", help="Query every source instead of stopping at the first hit")
    p.add_argument("--json", action="store_true", help="Print JSON")
    p.add_argument("--save", action="store_true", help="Write found emails back to lead_records (with --search-id)")
    return p.parse_args()


def _print_hit(name: str, company: str, hit) -> None:
    label = f"{name}" + (f" @ {company}" if company else "")
    if not hit:
        print(f"{label}")
        print("  email: —")
        print("  note: no public email found")
        print()
        return
    print(f"{label}")
    print(f"  email: {hit.email}")
    print(f"  source: {hit.source} · {hit.confidence}")
    if hit.evidence:
        print(f"  evidence: {hit.evidence}")
    print()


def lookup_person(name: str, args: argparse.Namespace) -> list[dict]:
    kwargs = dict(company=args.company, snippet=args.snippet, domain=args.domain)
    if args.all:
        hits = find_emails(name, all_sources=True, max_results=5, **kwargs)
    else:
        hit = find_email(name, **kwargs)
        hits = [hit] if hit else []
    if args.json:
        return [
            {
                "name": name,
                "company": args.company,
                **(h.to_dict() if h else {"email": None, "source": "", "confidence": "unknown", "evidence": "not found"}),
            }
            for h in (hits or [None])
        ]
    if hits:
        for hit in hits:
            _print_hit(name, args.company, hit)
    else:
        _print_hit(name, args.company, None)
    return []


def lookup_search(search_id: int, args: argparse.Namespace) -> list[dict]:
    from backend.database import SessionLocal
    from backend.models import LeadRecord

    db = SessionLocal()
    rows: list[dict] = []
    try:
        leads = (
            db.query(LeadRecord)
            .filter(LeadRecord.search_job_id == search_id)
            .order_by(LeadRecord.id)
            .limit(max(1, args.limit))
            .all()
        )
        if not leads:
            print(f"No leads found for search {search_id}.", file=sys.stderr)
            return []
        for lead in leads:
            if lead.email and not args.all:
                hit_data = {
                    "name": lead.name,
                    "company": lead.company_name or "",
                    "email": lead.email,
                    "source": "stored",
                    "confidence": "found",
                    "evidence": "already on lead record",
                    "id": lead.id,
                }
                rows.append(hit_data)
                if not args.json:
                    print(f"{lead.name}")
                    print(f"  email: {lead.email} (already stored)")
                    print()
                continue
            hit = find_email(
                lead.name,
                company=lead.company_name or args.company,
                snippet=lead.raw_snippet or "",
                domain=args.domain,
            )
            if hit and args.save:
                lead.email = hit.email
                db.commit()
            payload = {
                "id": lead.id,
                "name": lead.name,
                "company": lead.company_name or "",
                **(hit.to_dict() if hit else {"email": None, "source": "", "confidence": "unknown", "evidence": "not found"}),
            }
            rows.append(payload)
            if not args.json:
                _print_hit(lead.name, lead.company_name or "", hit)
        return rows
    finally:
        db.close()


def main() -> None:
    args = parse_args()
    name = (args.name_opt or args.name or "").strip()

    if args.search_id:
        rows = lookup_search(args.search_id, args)
        if args.json:
            print(json.dumps(rows, indent=2))
        return

    if not name:
        print("Provide a name, or --search-id.", file=sys.stderr)
        sys.exit(2)

    rows = lookup_person(name, args)
    if args.json:
        print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
