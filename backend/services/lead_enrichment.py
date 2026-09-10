from __future__ import annotations

import re
from typing import Any

from backend.config import OPENAI_API_KEY


def _extract_phone(text: str) -> str | None:
    m = re.search(r"(\+?\d[\d\s().-]{8,}\d)", text)
    return m.group(1).strip() if m else None


def _extract_email(text: str) -> str | None:
    m = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    return m.group(0) if m else None


def _extract_personal_location(title: str, body: str, fallback: str) -> str:
    text = f"{title} {body}"
    patterns = [
        r"([A-Za-z .'-]+,\s*[A-Za-z .'-]+(?:Metropolitan Area|Area|Region)?)",
        r"Based in\s+([A-Za-z .'-]+)",
        r"Location:\s*([A-Za-z .'-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            loc = m.group(1).strip(" .")
            if 3 < len(loc) < 80:
                return loc
    return fallback or "Not specified"


def _generate_ai_description(
    *,
    name: str,
    designation: str,
    company: str,
    field: str,
    snippet: str,
    prompt: str,
    activity: str,
) -> str:
    if OPENAI_API_KEY:
        try:
            import httpx

            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "system",
                            "content": "Write a concise 2-sentence marketing lead summary for sales outreach.",
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Lead: {name}, {designation} at {company} ({field}). "
                                f"Activity: {activity}. Brief: {prompt}. Snippet: {snippet[:300]}"
                            ),
                        },
                    ],
                    "max_tokens": 120,
                },
                timeout=20,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

    return (
        f"{name} is a {designation} at {company} in {field or 'their industry'}. "
        f"Profile appears {activity.lower()} on LinkedIn and aligns with your brief: "
        f"“{prompt[:90]}{'…' if len(prompt) > 90 else ''}”."
    )


def to_full_lead(raw: dict[str, Any], *, prompt: str, filters: dict[str, Any]) -> dict[str, Any]:
    title = raw.get("title") or raw.get("work_designation") or "Professional"
    company = raw.get("company") or raw.get("company_name") or "Unknown"
    field = raw.get("industry") or raw.get("work_field") or filters.get("industry") or "General"
    snippet = raw.get("snippet") or raw.get("raw_snippet") or ""
    text = f"{raw.get('name', '')} {title} {company} {snippet}"
    email = raw.get("email") or raw.get("email_guess") or _extract_email(text)
    phone = raw.get("phone") or _extract_phone(text)
    personal_loc = raw.get("personal_location") or raw.get("location") or _extract_personal_location(
        raw.get("name", ""), snippet, filters.get("location", "")
    )
    company_loc = filters.get("company_location") or filters.get("location") or personal_loc
    activity = raw.get("activity_status") or "Indexed"

    return {
        "name": raw.get("name") or "Unknown",
        "email": email,
        "phone": phone,
        "personal_location": personal_loc,
        "ai_description": _generate_ai_description(
            name=raw.get("name") or "This lead",
            designation=title,
            company=company,
            field=field,
            snippet=snippet,
            prompt=prompt,
            activity=activity,
        ),
        "work_field": field,
        "work_designation": title,
        "company_name": company,
        "company_location": company_loc,
        "linkedin_url": raw.get("linkedin_url") or raw.get("website") or raw.get("source"),
        "relevance_score": float(raw.get("relevance_score") or 0),
        "activity_status": activity,
        "raw_snippet": snippet[:500] if snippet else None,
        "tags": raw.get("tags") or [],
    }
