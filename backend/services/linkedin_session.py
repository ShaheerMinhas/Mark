from __future__ import annotations

import re
from datetime import datetime

import httpx

from backend.services.crypto import decrypt_value


def validate_linkedin_cookie(session_cookie: str) -> tuple[bool, str | None, str]:
    """
    Validate li_at cookie by requesting LinkedIn.
    Returns (is_valid, linkedin_name, message).
    """
    cookie = session_cookie.strip()
    if cookie.lower().startswith("li_at="):
        cookie = cookie.split("=", 1)[1].strip()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Cookie": f"li_at={cookie}; lang=v=2&lang=en-us",
    }
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get("https://www.linkedin.com/feed/", headers=headers)
            html = resp.text
            if resp.status_code != 200 or "authwall" in resp.url.path or "login" in resp.url.path:
                return False, None, "Session cookie invalid or expired"
            name = _extract_name(html)
            if name:
                return True, name, f"Connected as {name}"
            if "global-nav" in html or "feed-shared" in html:
                return True, "LinkedIn User", "Session validated successfully"
            return False, None, "Could not confirm LinkedIn session"
    except Exception as exc:
        return False, None, f"Validation failed: {exc}"


def _extract_name(html: str) -> str | None:
    patterns = [
        r'"firstName"\s*:\s*"([^"]+)"',
        r'"miniProfile"\s*:\s*\{[^}]*"firstName"\s*:\s*"([^"]+)"',
        r'data-member-id[^>]*>\s*<span[^>]*>([^<]{2,40})</span>',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1).strip()
    return None


def search_with_session(
    sales_nav_url: str,
    session_cookie: str,
    max_results: int = 25,
) -> tuple[list[dict], str]:
    """
    Attempt to fetch Sales Navigator search page with user session.
    Parses visible profile cards from HTML when possible.
    """
    cookie = session_cookie.strip()
    if cookie.lower().startswith("li_at="):
        cookie = cookie.split("=", 1)[1].strip()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Cookie": f"li_at={cookie}; lang=v=2&lang=en-us",
    }
    leads: list[dict] = []
    try:
        with httpx.Client(timeout=25, follow_redirects=True) as client:
            resp = client.get(sales_nav_url, headers=headers)
            if resp.status_code != 200:
                return [], f"Sales Nav request failed ({resp.status_code})"
            html = resp.text
            if "authwall" in str(resp.url) or "login" in str(resp.url):
                return [], "LinkedIn session expired — reconnect your cookie"

            # Parse profile links from Sales Nav HTML
            seen: set[str] = set()
            for m in re.finditer(r'href="(https?://[^"]*linkedin\.com/in/[^"?#]+)"', html):
                url = m.group(1).split("?")[0]
                if url in seen:
                    continue
                seen.add(url)
                slug = url.rstrip("/").split("/")[-1]
                name_guess = slug.replace("-", " ").title()
                leads.append(
                    {
                        "name": name_guess,
                        "title": "Professional",
                        "company": "From Sales Navigator",
                        "linkedin_url": url,
                        "snippet": "Extracted from LinkedIn Sales Navigator search with your session.",
                        "activity_status": "Session import",
                        "relevance_score": 0.85,
                        "tags": ["Sales Navigator", "Session"],
                    }
                )
                if len(leads) >= max_results:
                    break
            return leads, f"Imported {len(leads)} profiles from Sales Navigator"
    except Exception as exc:
        return [], f"Session search failed: {exc}"


def get_decrypted_cookie(encrypted: str) -> str:
    return decrypt_value(encrypted)
