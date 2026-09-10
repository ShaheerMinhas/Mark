#!/usr/bin/env python3
"""CLI: search marketing leads from a prompt and optional filters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from lead_finder import search_leads  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Find marketing leads from a prompt")
    p.add_argument("prompt", help="What leads you want for marketing")
    p.add_argument("--location", default="")
    p.add_argument("--industry", default="")
    p.add_argument("--company-size", default="")
    p.add_argument("--income-level", default="")
    p.add_argument("--gender", default="")
    p.add_argument("--language", default="")
    p.add_argument("--age-min", type=int, default=18)
    p.add_argument("--age-max", type=int, default=65)
    p.add_argument("--interests", default="", help="Comma-separated interests")
    p.add_argument("--job-titles", default="", help="Comma-separated job titles")
    p.add_argument("--max-results", type=int, default=12)
    p.add_argument("--json", action="store_true", help="Print raw JSON")
    p.add_argument("--include-inactive", action="store_true", help="Do not require active cues")
    p.add_argument("--include-non-business", action="store_true", help="Include profiles without clear company ties")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    result = search_leads(
        args.prompt,
        location=args.location,
        industry=args.industry,
        company_size=args.company_size,
        income_level=args.income_level,
        gender=args.gender,
        language=args.language,
        age_min=args.age_min,
        age_max=args.age_max,
        interests=[s.strip() for s in args.interests.split(",") if s.strip()],
        job_titles=[s.strip() for s in args.job_titles.split(",") if s.strip()],
        max_results=args.max_results,
        active_only=not args.include_inactive,
        business_only=not args.include_non_business,
    )

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(result.get("message", ""))
    print(f"Query: {result.get('query_used', '')}\n")
    for i, lead in enumerate(result.get("leads", []), 1):
        print(f"{i}. {lead['name']} — {lead['title']} @ {lead['company']}")
        print(f"   {lead.get('activity_status', '')} · {lead['location']} · {lead['relevance_score']*100:.0f}%")
        print(f"   {lead.get('linkedin_url') or lead.get('website')}")
        print(f"   {lead['snippet'][:140]}")
        print()


if __name__ == "__main__":
    main()
