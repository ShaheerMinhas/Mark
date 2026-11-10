"""Pluggable discovery sources. LinkedIn stays first. Every other source is off.

HTML pages use the pinned fetch and robots.txt. Official APIs use the pinned
fetch, a host allowlist, and their own key or User-Agent rule. A missing key
or a full budget means no call. Nothing here invents an email or a phone.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote_plus, urlparse

from sqlalchemy.orm import Session

from backend.models import DiscoveryProviderSecret, DiscoverySource, GlobalCompany, GlobalEvidence, GlobalPerson
from backend.services.crypto import decrypt_value, encrypt_value
from backend.services.discovery_engine.ssrf import SsrfError, pinned_opener, safe_fetch
from backend.services.EmailModule.robots import page_decision

_NAME = re.compile(r"\b([A-Z][a-z]{1,20} [A-Z][a-z]{1,20})\s*(?:,|-|–|—)\s*([A-Za-z][A-Za-z ]{1,40})")
_BLOCKED_HOSTS = {
    "zoominfo.com",
    "rocketreach.co",
    "linkedin.com",
    "facebook.com",
    "apollo.io",
    "lusha.com",
    "contactout.com",
    "signalhire.com",
    "maps.google.com",
    "google.com",
}

SOURCE_DEFAULTS = (
    ("linkedin", "LinkedIn public search", True, 10, 5, None),
    ("company_site", "Company websites", False, 20, 12, None),
    ("github", "GitHub public API", False, 30, 6, None),
    ("companies_house", "UK Companies House", False, 40, 5, None),
    ("sec_edgar", "SEC EDGAR", False, 50, 5, None),
    ("events", "Event and speaker pages", False, 60, 8, None),
    ("directories", "Approved business directories", False, 70, 6, None),
    ("google_places", "Google Places API", False, 80, 3, 0),
)


def ensure_sources(db: Session) -> list[DiscoverySource]:
    for name, label, enabled, priority, limit, monthly in SOURCE_DEFAULTS:
        row = db.get(DiscoverySource, name)
        if row is None:
            db.add(
                DiscoverySource(
                    name=name,
                    label=label,
                    enabled=enabled,
                    priority=priority,
                    max_requests_per_run=limit,
                    monthly_call_limit=monthly,
                    calls_used=0,
                    config={},
                )
            )
    db.flush()
    return db.query(DiscoverySource).order_by(DiscoverySource.priority.asc(), DiscoverySource.name.asc()).all()


def linkedin_enabled(db: Session) -> bool:
    rows = ensure_sources(db)
    linkedin = next(row for row in rows if row.name == "linkedin")
    return bool(linkedin.enabled)


def _two_part(value: str) -> str | None:
    parts = [part for part in re.split(r"\s+", (value or "").strip()) if part]
    if len(parts) != 2:
        return None
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z'\-]{1,20}", part) for part in parts):
        return None
    return f"{parts[0]} {parts[1]}"


def _hit(*, name: str, title: str | None, company: str | None, domain: str | None, profile: str | None, email: str | None, phone: str | None, source_url: str, provider: str) -> dict:
    return {
        "full_name": name,
        "title": title or None,
        "company_name": company or None,
        "domain": domain or None,
        "profile_url": profile or None,
        "email": email,
        "email_status": "found" if email else None,
        "email_confidence": "published" if email else None,
        "phone": phone,
        "phone_evidenced": bool(phone),
        "source_url": source_url,
        "provider": provider,
    }


def _people_from_html(html: str, *, domain: str, source_url: str, provider: str) -> list[dict]:
    from backend.services.discovery_engine.contacts import published_contact

    text = re.sub(r"<[^>]+>", " ", html or "")
    found = []
    seen = set()
    for match in _NAME.finditer(text):
        name = _two_part(match.group(1))
        if name is None or name in seen:
            continue
        seen.add(name)
        email, phone = published_contact(html, name=name, domain=domain)
        found.append(
            _hit(
                name=name,
                title=match.group(2).strip(),
                company=None,
                domain=domain,
                profile=None,
                email=email,
                phone=phone,
                source_url=source_url,
                provider=provider,
            )
        )
    return found


def _limited(fetch, limit: int):
    state = {"n": 0}

    def inner(url: str) -> bytes:
        if state["n"] >= limit:
            raise SsrfError("source request limit reached")
        state["n"] += 1
        return fetch(url)

    return inner


def collect_company_sites(db: Session, target, *, fetch, limit: int) -> tuple[list[dict], str]:
    codes = [str(code).upper() for code in (getattr(target, "countries", None) or []) if code]
    criteria = dict(getattr(target, "criteria", None) or {})
    codes += [str(code).upper() for code in ((criteria.get("geography") or {}).get("include_countries") or [])]
    query = db.query(GlobalCompany).filter(GlobalCompany.domain.isnot(None), GlobalCompany.archived_at.is_(None))
    if codes:
        query = query.filter(GlobalCompany.country_code.in_(codes))
    companies = query.order_by(GlobalCompany.id.asc()).limit(3).all()
    if not companies:
        return [], "Company websites had no stored domain to open."
    paths = ("/team", "/about", "/about-us", "/leadership", "/people", "/contact", "/contact-us", "/press")
    getter = _limited(fetch, limit)
    hits = []
    for company in companies:
        domain = (company.domain or "").lower().removeprefix("www.")
        origin = f"https://{domain}"
        for path in paths:
            url = origin + path
            try:
                if page_decision(url, fetch=getter) != "allow":
                    continue
                raw = getter(url)
            except SsrfError:
                break
            html = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            for hit in _people_from_html(html, domain=domain, source_url=url, provider="company_site"):
                hit["company_name"] = company.name
                hits.append(hit)
            if len(hits) >= 10:
                return hits, f"Company websites returned {len(hits)} named people."
    return hits, f"Company websites returned {len(hits)} named people."


def _secret(db: Session, name: str) -> str:
    row = db.get(DiscoveryProviderSecret, name)
    if row is None:
        return ""
    try:
        payload = json.loads(decrypt_value(row.ciphertext))
    except Exception:
        return ""
    return str(payload.get("api_key") or "")


def collect_github(db: Session, target, *, fetch, limit: int) -> tuple[list[dict], str]:
    token = _secret(db, "github")
    titles = list(getattr(target, "titles", None) or []) or ["cto"]
    query = quote_plus(str(titles[0]))
    getter = _limited(fetch, limit)
    try:
        search_url = f"https://api.github.com/search/users?q={query}&per_page=3"
        if page_decision(search_url, fetch=getter) != "allow":
            return [], "GitHub robots.txt did not allow the API host. No user was requested."
        raw = getter(search_url)
    except SsrfError as exc:
        return [], f"GitHub was not called ({exc})."
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return [], "GitHub did not return user JSON. No person was invented."
    if "rate limit" in str(payload.get("message") or "").lower():
        return [], "GitHub reported a rate limit. No further user was requested."
    hits = []
    for item in (payload.get("items") or [])[:3]:
        login = str(item.get("login") or "")
        if not login:
            continue
        try:
            user_raw = getter(f"https://api.github.com/users/{quote_plus(login)}")
        except SsrfError:
            break
        try:
            user = json.loads(user_raw.decode("utf-8") if isinstance(user_raw, bytes) else user_raw)
        except Exception:
            continue
        if "rate limit" in str(user.get("message") or "").lower():
            break
        name = _two_part(str(user.get("name") or ""))
        if name is None:
            continue
        email = str(user.get("email") or "").strip().lower() or None
        if email and "@" not in email:
            email = None
        hits.append(
            _hit(
                name=name,
                title=None,
                company=str(user.get("company") or "").strip() or None,
                domain=None,
                profile=f"https://github.com/{login}",
                email=email,
                phone=None,
                source_url=f"https://api.github.com/users/{login}",
                provider="github",
            )
        )
        hits[-1]["provider_record_id"] = login
    note = "A GitHub token is not configured. The public API was used." if not token else "GitHub used the saved token."
    return hits, f"{note} {len(hits)} named users had a public name."


def collect_companies_house(db: Session, target, *, fetch, limit: int) -> tuple[list[dict], str]:
    key = _secret(db, "companies_house")
    if not key:
        return [], "Companies House has no API key. No request was sent."
    titles = list(getattr(target, "titles", None) or []) or ["director"]
    getter = _limited(fetch, limit)
    url = f"https://api.company-information.service.gov.uk/search/officers?q={quote_plus(str(titles[0]))}&items_per_page=3"
    try:
        if page_decision(url, fetch=getter) != "allow":
            return [], "Companies House robots.txt did not allow the API. No request was sent."
        raw = getter(url)
    except SsrfError as exc:
        return [], f"Companies House was not called ({exc})."
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return [], "Companies House did not return officer JSON. No person was invented."
    hits = []
    for item in (payload.get("items") or [])[:3]:
        name = _two_part(str(item.get("title") or ""))
        if name is None:
            continue
        email = str(item.get("email") or "").strip().lower() or None
        if email and "@" not in email:
            email = None
        hits.append(
            _hit(
                name=name,
                title=None,
                company=str(item.get("description") or "").split(",")[0].strip() or None,
                domain=None,
                profile=None,
                email=email,
                phone=None,
                source_url="https://api.company-information.service.gov.uk/search/officers",
                provider="companies_house",
            )
        )
    return hits, f"Companies House returned {len(hits)} named officers. Emails stay empty unless the payload included one."


def collect_sec(db: Session, source: DiscoverySource, target, *, fetch, limit: int) -> tuple[list[dict], str]:
    contact = str((source.config or {}).get("sec_contact_email") or "").strip()
    if "@" not in contact:
        return [], "SEC EDGAR needs an operator contact email for the User-Agent. No request was sent."
    titles = list(getattr(target, "titles", None) or []) or ["cto"]
    getter = _limited(fetch, limit)
    url = f"https://efts.sec.gov/LATEST/search-index?q={quote_plus(str(titles[0]))}"
    try:
        if page_decision(url, fetch=getter) != "allow":
            return [], "SEC robots.txt did not allow the search path. No request was sent."
        raw = getter(url)
    except SsrfError as exc:
        return [], f"SEC EDGAR was not called ({exc})."
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return [], "SEC EDGAR did not return filing JSON. No person was invented."
    hits = []
    for item in ((payload.get("hits") or {}).get("hits") or [])[:5]:
        names = ((item or {}).get("_source") or {}).get("display_names") or []
        for raw_name in names:
            flipped = _sec_name(str(raw_name))
            if flipped is None or any(hit["full_name"] == flipped for hit in hits):
                continue
            hits.append(
                _hit(
                    name=flipped,
                    title=None,
                    company=None,
                    domain=None,
                    profile=None,
                    email=None,
                    phone=None,
                    source_url="https://efts.sec.gov/LATEST/search-index",
                    provider="sec_edgar",
                )
            )
    return hits, f"SEC EDGAR returned {len(hits)} officer names. No email is present in that search payload."


def _sec_name(value: str) -> str | None:
    head = value.split("(")[0].strip()
    if "," not in head:
        return _two_part(head)
    last, first = [part.strip() for part in head.split(",", 1)]
    return _two_part(f"{first} {last}")


def collect_events(target, *, fetch, limit: int) -> tuple[list[dict], str]:
    titles = list(getattr(target, "titles", None) or []) or ["cto"]
    query = quote_plus(f'speaker conference "{titles[0]}"')
    getter = _limited(fetch, limit)
    search = f"https://html.duckduckgo.com/html/?q={query}"
    try:
        if page_decision(search, fetch=getter) != "allow":
            return [], "Search robots.txt did not allow an event query. No speaker page was opened."
        raw = getter(search)
    except SsrfError as exc:
        return [], f"Event search was not called ({exc})."
    html = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    hrefs = re.findall(r'href="(https?://[^"]+)"', html)
    hits = []
    opened = 0
    for href in hrefs:
        host = (urlparse(href).hostname or "").lower()
        if "linkedin.com" in host or "duckduckgo.com" in host:
            continue
        if opened >= 3:
            break
        try:
            if page_decision(href, fetch=getter) != "allow":
                continue
            page = getter(href)
        except SsrfError:
            break
        opened += 1
        text = page.decode("utf-8", errors="replace") if isinstance(page, bytes) else str(page)
        hits.extend(_people_from_html(text, domain=host, source_url=href, provider="events"))
    return hits, f"Event pages returned {len(hits)} named people. LinkedIn results were left to the LinkedIn source."


def collect_directories(source: DiscoverySource, *, fetch, limit: int) -> tuple[list[dict], str]:
    hosts = [str(host).strip().lower() for host in ((source.config or {}).get("approved_hosts") or []) if str(host).strip()]
    if not hosts:
        return [], "No directory host has been approved. No directory page was opened."
    getter = _limited(fetch, limit)
    hits = []
    for host in hosts[:3]:
        if any(host == blocked or host.endswith("." + blocked) for blocked in _BLOCKED_HOSTS):
            continue
        url = f"https://{host}/"
        try:
            if page_decision(url, fetch=getter) != "allow":
                continue
            raw = getter(url)
        except SsrfError:
            break
        html = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        hits.extend(_people_from_html(html, domain=host, source_url=url, provider="directories"))
    return hits, f"Approved directories returned {len(hits)} named people."


def collect_google_places(db: Session, source: DiscoverySource, target, *, fetch, limit: int) -> tuple[list[dict], str]:
    key = _secret(db, "google_places")
    monthly = int(source.monthly_call_limit or 0)
    if not key or monthly < 1 or int(source.calls_used or 0) >= monthly:
        return [], "Google Places needs a saved key and a remaining call budget. No request was sent."
    titles = list(getattr(target, "titles", None) or []) or ["technology"]
    getter = _limited(fetch, limit)
    url = f"https://maps.googleapis.com/maps/api/place/textsearch/json?query={quote_plus(str(titles[0]))}"
    try:
        if page_decision(url, fetch=getter) != "allow":
            return [], "Google robots.txt did not allow the Places host. No request was sent."
        source.calls_used = int(source.calls_used or 0) + 1
        db.flush()
        raw = getter(url)
    except SsrfError as exc:
        return [], f"Google Places was not called ({exc})."
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return [], "Google Places did not return JSON. No person was invented."
    hits = []
    for item in (payload.get("results") or [])[:3]:
        name = _two_part(str(item.get("contact_name") or ""))
        if name is None:
            continue
        phone = str(item.get("formatted_phone_number") or "").strip() or None
        hits.append(
            _hit(
                name=name,
                title=None,
                company=str(item.get("name") or "").strip() or None,
                domain=None,
                profile=None,
                email=None,
                phone=phone,
                source_url="https://maps.googleapis.com/maps/api/place/textsearch/json",
                provider="google_places",
            )
        )
    if not hits:
        return [], "Google Places returned businesses without a person name. No person was created."
    return hits, f"Google Places returned {len(hits)} named contacts."


_COLLECTORS = {
    "company_site": lambda db, source, target, fetch, limit: collect_company_sites(db, target, fetch=fetch, limit=limit),
    "github": lambda db, source, target, fetch, limit: collect_github(db, target, fetch=fetch, limit=limit),
    "companies_house": lambda db, source, target, fetch, limit: collect_companies_house(db, target, fetch=fetch, limit=limit),
    "sec_edgar": lambda db, source, target, fetch, limit: collect_sec(db, source, target, fetch=fetch, limit=limit),
    "events": lambda db, source, target, fetch, limit: collect_events(target, fetch=fetch, limit=limit),
    "directories": lambda db, source, target, fetch, limit: collect_directories(source, fetch=fetch, limit=limit),
    "google_places": lambda db, source, target, fetch, limit: collect_google_places(db, source, target, fetch=fetch, limit=limit),
}


def _fetch_for(db: Session, source: DiscoverySource):
    """Pinned GET for one source. Official APIs add their key or User-Agent here."""
    import os
    from urllib.parse import urlparse as parse_url

    token = _secret(db, source.name) if source.name in {"github", "companies_house", "google_places"} else ""
    contact = str((source.config or {}).get("sec_contact_email") or "")

    def fetch(url: str) -> bytes:
        if os.environ.get("MARK_DISABLE_CONTACT_NETWORK") == "1":
            raise SsrfError("page fetch is disabled in this process")
        target_url = url
        headers: dict[str, str] = {}
        user_agent = "MarkBot/1.0"
        if source.name == "github" and token:
            headers["Authorization"] = f"Bearer {token}"
        if source.name == "companies_house" and token:
            import base64

            headers["Authorization"] = "Basic " + base64.b64encode(f"{token}:".encode()).decode()
        if source.name == "google_places" and token and "place/textsearch" in url and "key=" not in url:
            target_url = url + "&key=" + quote_plus(token)
        if source.name == "sec_edgar" and "@" in contact:
            user_agent = f"Mark Discovery {contact}"
        host = (parse_url(target_url).hostname or "").strip(".").lower()

        def opener(request_url, addresses, timeout, max_bytes):
            return pinned_opener(
                request_url,
                addresses,
                timeout,
                max_bytes,
                user_agent=user_agent,
                extra_headers=headers,
            )

        return safe_fetch(target_url, opener=opener, allowed_hosts={host}, timeout=8, max_bytes=200_000)

    return fetch


def collect_extra_sources(db: Session, target, *, fetch=None) -> tuple[list[dict], str]:
    """Run enabled sources other than LinkedIn, in priority order."""
    notes = []
    hits = []
    for source in ensure_sources(db):
        if source.name == "linkedin" or not source.enabled:
            continue
        collector = _COLLECTORS.get(source.name)
        if collector is None:
            continue
        getter = fetch or _fetch_for(db, source)
        found, note = collector(db, source, target, getter, int(source.max_requests_per_run or 5))
        hits.extend(found)
        notes.append(note)
    if not notes:
        return [], "No additional discovery source is enabled."
    return hits, " ".join(notes)


def source_report(db: Session) -> list[dict]:
    rows = ensure_sources(db)
    providers = {
        "linkedin": "public_serp",
        "company_site": "company_site",
        "github": "github",
        "companies_house": "companies_house",
        "sec_edgar": "sec_edgar",
        "events": "events",
        "directories": "directories",
        "google_places": "google_places",
    }
    report = []
    for row in rows:
        provider = providers.get(row.name, row.name)
        person_ids = [
            item
            for (item,) in db.query(GlobalEvidence.subject_id)
            .filter(GlobalEvidence.subject_type == "person", GlobalEvidence.provider == provider)
            .distinct()
            .all()
        ]
        people = db.query(GlobalPerson).filter(GlobalPerson.id.in_(person_ids), GlobalPerson.archived_at.is_(None)).all() if person_ids else []
        report.append(
            {
                "name": row.name,
                "label": row.label,
                "enabled": bool(row.enabled),
                "priority": int(row.priority or 100),
                "max_requests_per_run": int(row.max_requests_per_run or 5),
                "monthly_call_limit": row.monthly_call_limit,
                "calls_used": int(row.calls_used or 0),
                "approved_hosts": list((row.config or {}).get("approved_hosts") or []),
                "sec_contact_configured": bool((row.config or {}).get("sec_contact_email")),
                "credential_configured": bool(_secret(db, row.name)),
                "people": len(people),
                "with_email": sum(1 for person in people if person.email),
                "with_phone": sum(1 for person in people if person.phone),
            }
        )
    return report


def save_source_secret(db: Session, name: str, api_key: str) -> None:
    if name not in {item[0] for item in SOURCE_DEFAULTS}:
        raise ValueError("Unknown source")
    payload = json.dumps({"api_key": api_key.strip()})
    row = db.get(DiscoveryProviderSecret, name)
    if row is None:
        db.add(DiscoveryProviderSecret(provider_name=name, ciphertext=encrypt_value(payload)))
    else:
        row.ciphertext = encrypt_value(payload)
    db.commit()


def update_source(db: Session, name: str, body: dict) -> DiscoverySource:
    row = db.get(DiscoverySource, name)
    if row is None:
        ensure_sources(db)
        row = db.get(DiscoverySource, name)
    if row is None:
        raise ValueError("Unknown source")
    if "enabled" in body:
        row.enabled = bool(body["enabled"])
    if "priority" in body:
        priority = int(body["priority"])
        if not 1 <= priority <= 1000:
            raise ValueError("priority must be 1..1000")
        row.priority = priority
    if "max_requests_per_run" in body:
        limit = int(body["max_requests_per_run"])
        if not 1 <= limit <= 20:
            raise ValueError("max_requests_per_run must be 1..20")
        row.max_requests_per_run = limit
    if "monthly_call_limit" in body and body["monthly_call_limit"] is not None:
        monthly = int(body["monthly_call_limit"])
        if monthly < 0 or monthly > 10000:
            raise ValueError("monthly_call_limit must be 0..10000")
        row.monthly_call_limit = monthly
    config = dict(row.config or {})
    if "approved_hosts" in body:
        hosts = []
        for host in body["approved_hosts"] or []:
            cleaned = str(host).strip().lower().removeprefix("www.")
            if not cleaned or "/" in cleaned or " " in cleaned:
                raise ValueError("directory hosts are hostnames only")
            if any(cleaned == blocked or cleaned.endswith("." + blocked) for blocked in _BLOCKED_HOSTS):
                raise ValueError(f"{cleaned} is not an approved directory host")
            hosts.append(cleaned)
        config["approved_hosts"] = hosts
    if "sec_contact_email" in body:
        email = str(body.get("sec_contact_email") or "").strip()
        if email and "@" not in email:
            raise ValueError("sec_contact_email must be an email address")
        config["sec_contact_email"] = email
    row.config = config
    if body.get("reset_calls"):
        row.calls_used = 0
    if body.get("api_key"):
        save_source_secret(db, name, str(body["api_key"]))
    db.commit()
    db.refresh(row)
    return row
