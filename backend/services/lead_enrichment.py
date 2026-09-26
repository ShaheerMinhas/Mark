from __future__ import annotations

import re
from typing import Any

from backend.config import OPENAI_API_KEY
from backend.services.CompanyModule.extract import extract_profile_geo
from backend.services.CompanyModule.finder import is_usable_company
from backend.services.EmailModule.extract import extract_first_email

_GENERIC_COMPANIES = {
    "",
    "business",
    "company",
    "unknown",
    "n/a",
    "na",
    "none",
    "linkedin",
    "linkedin member",
    "professional",
    "self-employed",
    "self employed",
    "freelancer",
    "independent",
    "not specified",
    "general",
}

_KNOWN_INDUSTRIES = {
    "technology",
    "healthcare",
    "finance",
    "retail",
    "education",
    "real estate",
    "manufacturing",
    "hospitality",
    "marketing & advertising",
    "professional services",
}


def _extract_phone(text: str) -> str | None:
    if not text:
        return None
    for match in re.finditer(r"(\+?\d[\d\s().-]{8,}\d)", text):
        raw = re.sub(r"\s+", " ", match.group(1)).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 10 or len(digits) > 15:
            continue
        if digits.startswith("20") and len(digits) <= 12 and re.fullmatch(r"(?:20\d{2})+", digits):
            continue
        return raw
    return None


def _clean_company_name(raw: str | None) -> str:
    """Keep real employer names; drop generic placeholders. Do not invent values."""
    if not raw:
        return ""
    company = re.sub(r"\s+", " ", str(raw)).strip(" ·|-–,;:()[]{}\"'")
    company = re.sub(r"\s*\|\s*LinkedIn.*$", "", company, flags=re.I).strip()
    masked = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", company)
    if re.search(r"\s+at\s+|\s+@", masked, re.I):
        company = re.split(r"\s+at\s+|\s+@\s*|\s+@", masked, maxsplit=1, flags=re.I)[-1].strip()
    if " · " in company:
        company = company.split(" · ", 1)[0].strip()
    if re.fullmatch(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", company):
        return ""
    if company.lower() in _GENERIC_COMPANIES:
        return ""
    if not is_usable_company(company):
        return ""
    if len(company) < 3 or len(company) > 80:
        return ""
    if re.search(r"\b(Area|Region|Metropolitan|Remote|Worldwide|Wikipedia|LinkedIn)\b", company, re.I):
        return ""
    if "," in company and len(company) < 60 and not re.search(r"\b(Inc\.?|LLC|Ltd\.?|Corp\.?)\b", company, re.I):
        return ""
    return company


def _extract_personal_location(snippet: str) -> str:
    return extract_profile_geo(snippet)


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
                            "content": "Write a concise 2-sentence marketing lead summary for sales outreach. Do not invent employer, email, or location facts.",
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Lead: {name}"
                                f"{f', {designation}' if designation else ''}"
                                f"{f' at {company}' if company else ''}"
                                f"{f' ({field})' if field else ''}. "
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

    bits = [name or "This lead"]
    if designation:
        bits.append(f"is a {designation}")
    else:
        bits.append("appears in public LinkedIn search")
    if company:
        bits.append(f"at {company}")
    sentence = " ".join(bits) + "."
    extra = ""
    if snippet:
        extra = f" Public snippet: {snippet[:140].rstrip()}."
    return f"{sentence}{extra}"


def to_full_lead(raw: dict[str, Any], *, prompt: str, filters: dict[str, Any]) -> dict[str, Any]:
    title = (raw.get("title") or raw.get("work_designation") or "").strip()
    snippet = raw.get("snippet") or raw.get("raw_snippet") or ""
    name = (raw.get("name") or "").strip() or "Unknown"
    company = _clean_company_name(raw.get("company") or raw.get("company_name"))

    scraped_field = (raw.get("industry") or raw.get("work_field") or "").strip()
    filter_field = str(filters.get("industry") or "").strip()
    if scraped_field and scraped_field.lower() not in {"general", "not specified"}:
        field = scraped_field
    elif filter_field.lower() in _KNOWN_INDUSTRIES:
        field = filter_field
    else:
        field = ""

    email = raw.get("email") or extract_first_email(snippet, name=name if name != "Unknown" else "")
    phone = raw.get("phone") or _extract_phone(snippet)
    personal_loc = raw.get("personal_location") or _extract_personal_location(snippet)
    if not personal_loc:
        loc = (raw.get("location") or "").strip()
        if loc and loc.lower() not in {"not specified", "remote / multi-market"}:
            personal_loc = loc

    activity = raw.get("activity_status") or "Indexed"

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "personal_location": personal_loc or None,
        "ai_description": _generate_ai_description(
            name=name if name != "Unknown" else "This lead",
            designation=title,
            company=company,
            field=field,
            snippet=snippet,
            prompt=prompt,
            activity=activity,
        ),
        "work_field": field or None,
        "work_designation": title or None,
        "company_name": company or None,
        "company_location": None,
        "linkedin_url": raw.get("linkedin_url") or raw.get("website") or raw.get("source"),
        "relevance_score": float(raw.get("relevance_score") or 0),
        "activity_status": activity,
        "raw_snippet": snippet[:500] if snippet else None,
        "tags": raw.get("tags") or [],
    }
