"""LinkedIn search engines for one autonomous query.

Google Programmable Search, Brave, Serper, and SerpAPI run together when they
are enabled and have a key. DuckDuckGo HTML is the last resort. A 403, a 429,
a timeout, or a missing key is recorded and the other engines still run.
Keys are encrypted provider secrets. Environment variables fill a gap when no
secret has been saved. Nothing here invents an email or a phone number.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote_plus, urlparse

from sqlalchemy.orm import Session

from backend.models import DiscoveryProviderSecret, DiscoverySearchBackend, ProviderUsage
from backend.services.crypto import decrypt_value, encrypt_value
from backend.services.discovery.extract import extract_candidate
from backend.services.discovery_engine.public_source import (
    SEARCH_HOST,
    advance_cursor,
    hits_from_html,
    next_query,
    person_from_candidate,
    robots_page_allows,
)
from backend.services.discovery_engine.ssrf import SEARCH_API_HOSTS, SsrfError, pinned_opener, safe_fetch
from backend.services.identity import normalize_linkedin_url

logger = logging.getLogger("mark.discovery.search")

BACKENDS = (
    ("google_cse", "Google Programmable Search", False, 10, 5, 100),
    ("brave", "Brave Search", False, 20, 5, 100),
    ("serper", "Serper", False, 30, 5, 100),
    ("serpapi", "SerpAPI", False, 40, 5, 100),
    ("duckduckgo", "DuckDuckGo HTML", True, 100, 5, 100),
)

_ENV = {
    "google_cse": ("GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX"),
    "brave": ("BRAVE_SEARCH_API_KEY", None),
    "serper": ("SERPER_API_KEY", None),
    "serpapi": ("SERPAPI_API_KEY", None),
}

_SHORT = {
    "google_cse": "Google CSE",
    "brave": "Brave",
    "serper": "Serper",
    "serpapi": "SerpAPI",
    "duckduckgo": "DuckDuckGo",
}

_CALL_HOSTS = SEARCH_API_HOSTS | {SEARCH_HOST}
_QUOTA_TOKENS = (
    "quota",
    "daily limit",
    "dailylimit",
    "rate limit",
    "ratelimit",
    "billing",
    "exceeded",
    "out of searches",
    "run out of searches",
)


def ensure_search_backends(db: Session) -> list[DiscoverySearchBackend]:
    for name, label, enabled, priority, limit, daily in BACKENDS:
        row = db.get(DiscoverySearchBackend, name)
        if row is None:
            db.add(
                DiscoverySearchBackend(
                    name=name,
                    label=label,
                    enabled=enabled,
                    priority=priority,
                    max_requests_per_run=limit,
                    daily_cap=daily,
                    calls_today=0,
                    calls_day="",
                    last_status="",
                    last_detail="",
                )
            )
        elif row.label != label:
            row.label = label
    db.flush()
    return (
        db.query(DiscoverySearchBackend)
        .order_by(DiscoverySearchBackend.priority.asc(), DiscoverySearchBackend.name.asc())
        .all()
    )


def _token(value: str) -> str:
    return "".join(ch for ch in str(value or "") if 32 <= ord(ch) < 127).strip()


def _stored_secret(db: Session | None, name: str) -> dict:
    if db is None:
        return {}
    row = db.get(DiscoveryProviderSecret, name)
    if row is None or not row.ciphertext:
        return {}
    try:
        payload = json.loads(decrypt_value(row.ciphertext))
    except Exception:
        logger.warning("search backend %s credential could not be read", name)
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_credentials(db: Session | None, name: str) -> dict:
    stored = _stored_secret(db, name)
    key_env, cx_env = _ENV.get(name, (None, None))
    saved_key = _token(stored.get("api_key") or "")
    saved_cx = _token(stored.get("cx") or "")
    env_key = _token(os.environ.get(key_env, "")) if key_env else ""
    env_cx = _token(os.environ.get(cx_env, "")) if cx_env else ""
    return {
        "api_key": saved_key or env_key,
        "cx": saved_cx or env_cx,
        "saved": bool(saved_key),
        "cx_saved": bool(saved_cx),
        "env_key": bool(env_key),
        "env_cx": bool(env_cx),
    }


def credentials_ready(name: str, creds: dict) -> bool:
    if name == "duckduckgo":
        return True
    if not creds.get("api_key"):
        return False
    if name == "google_cse" and not creds.get("cx"):
        return False
    return True


def _save_secret(db: Session, name: str, *, api_key: str | None, cx: str | None) -> None:
    current = _stored_secret(db, name)
    if api_key:
        current["api_key"] = _token(api_key)
    if cx:
        current["cx"] = _token(cx)
    current.pop("secret", None)
    if not current.get("api_key") and not current.get("cx"):
        return
    payload = json.dumps({"api_key": current.get("api_key") or "", "cx": current.get("cx") or ""})
    row = db.get(DiscoveryProviderSecret, name)
    if row is None:
        db.add(DiscoveryProviderSecret(provider_name=name, ciphertext=encrypt_value(payload)))
    else:
        row.ciphertext = encrypt_value(payload)
        row.updated_at = datetime.utcnow()
    db.flush()


def _calls_today(row: DiscoverySearchBackend, today: str) -> int:
    if (row.calls_day or "") != today:
        return 0
    return int(row.calls_today or 0)


def _remember(row: DiscoverySearchBackend, *, status: str, detail: str, counted: bool, today: str) -> None:
    if counted:
        if (row.calls_day or "") != today:
            row.calls_day = today
            row.calls_today = 0
        row.calls_today = int(row.calls_today or 0) + 1
    if status:
        row.last_status = status[:32]
    row.last_detail = (detail or "")[:500]
    row.updated_at = datetime.utcnow()


def _redact(text: str, secrets: list[str]) -> str:
    cleaned = text or ""
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "redacted")
    cleaned = re.sub(r"(?i)(api[_-]?key|key|token|cx)=([^&\s]+)", r"\1=redacted", cleaned)
    return cleaned[:300]


def search_backend_report(db: Session, *, commit: bool = False) -> list[dict]:
    today = datetime.utcnow().date().isoformat()
    items = []
    for row in ensure_search_backends(db):
        creds = resolve_credentials(db, row.name)
        status = row.last_status or ""
        if row.name != "duckduckgo" and not credentials_ready(row.name, creds) and not status:
            status = "no_key"
        items.append(
            {
                "name": row.name,
                "label": row.label,
                "enabled": bool(row.enabled),
                "priority": int(row.priority or 100),
                "max_requests_per_run": int(row.max_requests_per_run or 0),
                "daily_cap": int(row.daily_cap or 0),
                "calls_today": _calls_today(row, today),
                "last_status": status,
                "last_detail": row.last_detail or "",
                "credential_configured": bool(creds["saved"]),
                "cx_configured": bool(creds["cx_saved"]),
                "env_key": bool(creds["env_key"]),
                "env_cx": bool(creds["env_cx"]),
                "needs_key": row.name != "duckduckgo",
                "needs_cx": row.name == "google_cse",
            }
        )
    if commit:
        db.commit()
    return items


def update_search_backend(db: Session, name: str, body: dict) -> None:
    known = {item[0] for item in BACKENDS}
    if name not in known:
        raise ValueError("Unknown search backend")
    ensure_search_backends(db)
    row = db.get(DiscoverySearchBackend, name)
    if row is None:
        raise ValueError("Unknown search backend")
    if "enabled" in body and body["enabled"] is not None:
        row.enabled = bool(body["enabled"])
    if "max_requests_per_run" in body and body["max_requests_per_run"] is not None:
        limit = int(body["max_requests_per_run"])
        if not 0 <= limit <= 50:
            raise ValueError("max_requests_per_run must be 0..50")
        row.max_requests_per_run = limit
    if "daily_cap" in body and body["daily_cap"] is not None:
        cap = int(body["daily_cap"])
        if not 0 <= cap <= 100000:
            raise ValueError("daily_cap must be 0..100000")
        row.daily_cap = cap
    api_key = _token(body.get("api_key") or "") if body.get("api_key") else ""
    cx = _token(body.get("cx") or "") if body.get("cx") else ""
    if name == "duckduckgo" and (api_key or cx):
        raise ValueError("DuckDuckGo does not take a key")
    if api_key or cx:
        _save_secret(db, name, api_key=api_key or None, cx=cx or None)
        if credentials_ready(name, resolve_credentials(db, name)):
            row.enabled = True
    row.updated_at = datetime.utcnow()
    db.commit()


@dataclass
class PreparedCall:
    name: str
    label: str
    host: str
    method: str
    url: str = field(repr=False)
    source_url: str = field(repr=False)
    headers: dict = field(repr=False)
    body: bytes | None = field(repr=False)
    secrets: list[str] = field(default_factory=list, repr=False)


@dataclass
class BackendOutcome:
    name: str
    label: str
    status: str
    detail: str
    hits: list
    called: bool
    http_status: int
    skip: str = ""


def _request_for(name: str, query: str, creds: dict) -> PreparedCall:
    quoted = quote_plus(query)
    secrets = [value for value in (creds.get("api_key"), creds.get("cx")) if value]
    if name == "google_cse":
        url = (
            "https://www.googleapis.com/customsearch/v1"
            f"?key={quote_plus(creds['api_key'])}&cx={quote_plus(creds['cx'])}&q={quoted}&num=10"
        )
        return PreparedCall(
            name, _SHORT[name], "www.googleapis.com", "GET", url,
            "https://www.googleapis.com/customsearch/v1", {}, None, secrets,
        )
    if name == "brave":
        url = f"https://api.search.brave.com/res/v1/web/search?q={quoted}&count=10"
        return PreparedCall(
            name, _SHORT[name], "api.search.brave.com", "GET", url,
            "https://api.search.brave.com/res/v1/web/search",
            {"X-Subscription-Token": creds["api_key"], "Accept": "application/json"},
            None, secrets,
        )
    if name == "serper":
        return PreparedCall(
            name, _SHORT[name], "google.serper.dev", "POST", "https://google.serper.dev/search",
            "https://google.serper.dev/search",
            {"X-API-KEY": creds["api_key"], "Content-Type": "application/json"},
            json.dumps({"q": query, "num": 10}).encode(), secrets,
        )
    if name == "serpapi":
        url = (
            "https://serpapi.com/search.json"
            f"?engine=google&q={quoted}&api_key={quote_plus(creds['api_key'])}&num=10"
        )
        return PreparedCall(
            name, _SHORT[name], "serpapi.com", "GET", url,
            "https://serpapi.com/search.json", {}, None, secrets,
        )
    url = f"https://{SEARCH_HOST}/html/?q={quoted}"
    return PreparedCall(name, _SHORT[name], SEARCH_HOST, "GET", url, url, {}, None, [])


def _http(
    call: PreparedCall,
    *,
    transport=None,
    fetcher=None,
) -> tuple[int, bytes, str]:
    host = (urlparse(call.url).hostname or "").strip(".").lower()
    if host not in _CALL_HOSTS:
        return 0, b"", "host is outside the allowed set"
    if fetcher is not None and call.name == "duckduckgo":
        try:
            raw = fetcher(call.url)
        except SsrfError as exc:
            return 0, b"", str(exc)
        except Exception:
            return 0, b"", "request failed"
        data = raw if isinstance(raw, bytes) else str(raw).encode()
        return 200, data, ""
    if transport is not None:
        try:
            status, raw = transport(call.name, call.url, call.method, dict(call.headers), call.body)
        except Exception:
            return 0, b"", "request failed"
        data = raw if isinstance(raw, bytes) else str(raw or "").encode()
        return int(status or 0), data, ""
    if os.environ.get("MARK_DISABLE_CONTACT_NETWORK") == "1":
        return 0, b"", "search fetch is disabled in this process"
    captured = {"status": 0}

    def opener(request_url, addresses, timeout, max_bytes):
        status, location, raw, headers = pinned_opener(
            request_url,
            addresses,
            timeout,
            max_bytes,
            extra_headers=call.headers or None,
            method=call.method,
            body=call.body,
        )
        captured["status"] = status
        return status, location, raw, headers

    try:
        data = safe_fetch(
            call.url,
            opener=opener,
            allowed_hosts={host},
            timeout=8,
            max_bytes=300_000,
        )
    except SsrfError as exc:
        return 0, b"", str(exc)
    except Exception:
        return 0, b"", "request failed"
    body = data if isinstance(data, bytes) else str(data or "").encode()
    return int(captured["status"] or 0), body, ""


def _quota_blob(payload, raw: str) -> bool:
    parts = [raw or ""]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            parts.append(str(err.get("message") or ""))
            parts.append(str(err.get("status") or ""))
            for item in err.get("errors") or []:
                if isinstance(item, dict):
                    parts.append(str(item.get("reason") or ""))
                    parts.append(str(item.get("message") or ""))
        elif err:
            parts.append(str(err))
        parts.append(str(payload.get("message") or ""))
    blob = " ".join(parts).lower()
    return any(token in blob for token in _QUOTA_TOKENS)


def _payload(body: bytes):
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace") or "null")
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _rows(name: str, payload: dict) -> list[tuple[str, str, str]]:
    found = []
    if name == "google_cse":
        items = payload.get("items") or []
        for item in items:
            found.append((item.get("title") or "", item.get("snippet") or "", item.get("link") or ""))
    elif name == "brave":
        for item in ((payload.get("web") or {}).get("results") or []):
            found.append((item.get("title") or "", item.get("description") or "", item.get("url") or ""))
    elif name == "serper":
        for item in payload.get("organic") or []:
            found.append((item.get("title") or "", item.get("snippet") or "", item.get("link") or ""))
    elif name == "serpapi":
        for item in payload.get("organic_results") or []:
            found.append((item.get("title") or "", item.get("snippet") or "", item.get("link") or ""))
    return found


def _error_only(name: str, payload: dict | None) -> bool:
    if not isinstance(payload, dict) or not payload.get("error"):
        return False
    return not _rows(name, payload)


def _hits_from_json(name: str, payload: dict, *, source_url: str) -> list[dict]:
    hits = []
    for title, snippet, url in _rows(name, payload):
        parsed = extract_candidate({"href": url or "", "title": title or "", "body": snippet or ""})
        if parsed is None or not parsed.name:
            continue
        hits.append(person_from_candidate(parsed, backend=name, source_url=source_url))
    return hits


def _classify(name: str, status: int, body: bytes, error: str) -> tuple[str, str, list[dict], dict | None]:
    if error and status == 0:
        return "blocked", error, [], None
    payload = None if name == "duckduckgo" else _payload(body)
    raw = "" if name == "duckduckgo" else body.decode("utf-8", errors="replace")
    if status == 429 or (status >= 400 and _quota_blob(payload, raw)) or (status == 200 and _error_only(name, payload) and _quota_blob(payload, raw)):
        detail = f"HTTP {status}" if status else "quota"
        return "quota", detail, [], payload
    if status in {401, 403} or status >= 400 or status == 0 or (status == 200 and _error_only(name, payload)):
        detail = f"HTTP {status}" if status else "request failed"
        return "blocked", detail, [], payload
    if name == "duckduckgo":
        return "ok", "", [], None
    if payload is None:
        return "blocked", "response was not JSON", [], None
    return "ok", "", [], payload


def _execute_duckduckgo(call: PreparedCall, query: str, *, transport, fetcher) -> BackendOutcome:
    robots = PreparedCall(
        call.name, call.label, SEARCH_HOST, "GET",
        f"https://{SEARCH_HOST}/robots.txt", f"https://{SEARCH_HOST}/robots.txt", {}, None, [],
    )
    status, body, error = _http(robots, transport=transport, fetcher=fetcher)
    if error:
        return BackendOutcome(call.name, call.label, "blocked", error, [], False, status)
    if status >= 400 or not robots_page_allows(body):
        return BackendOutcome(
            call.name, call.label, "blocked", "robots.txt refused the fetch", [], False, status, skip="robots"
        )
    status, body, error = _http(call, transport=transport, fetcher=fetcher)
    kind, detail, _ignored, _payload_value = _classify("duckduckgo", status, body, error)
    hits = []
    if kind == "ok":
        html = body.decode("utf-8", errors="replace")
        hits = hits_from_html(html, query=query)
    if kind != "ok":
        logger.warning("search backend duckduckgo %s (%s)", kind, detail)
    return BackendOutcome(call.name, call.label, kind, detail or f"{len(hits)} hits", hits, True, status)


def _execute_api(call: PreparedCall, *, transport) -> BackendOutcome:
    status, body, error = _http(call, transport=transport, fetcher=None)
    kind, detail, _ignored, payload = _classify(call.name, status, body, error)
    detail = _redact(detail, call.secrets)
    hits = []
    if kind == "ok" and isinstance(payload, dict):
        hits = _hits_from_json(call.name, payload, source_url=call.source_url)
        detail = f"{len(hits)} hits"
    if kind != "ok":
        logger.warning("search backend %s %s (%s)", call.name, kind, detail)
    return BackendOutcome(call.name, call.label, kind, detail, hits, True, status)


def _run_call(call: PreparedCall, query: str, *, transport, fetcher) -> BackendOutcome:
    try:
        if call.name == "duckduckgo":
            return _execute_duckduckgo(call, query, transport=transport, fetcher=fetcher)
        return _execute_api(call, transport=transport)
    except Exception:
        logger.warning("search backend %s failed", call.name)
        return BackendOutcome(call.name, call.label, "blocked", "request failed", [], True, 0)


def merge_profile_hits(groups: list[tuple[str, list[dict]]]) -> list[dict]:
    """One row per normalized LinkedIn profile. Later engines fill empty fields only."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for backend, hits in groups:
        for hit in hits or []:
            url = normalize_linkedin_url(hit.get("profile_url") or "")
            if not url:
                continue
            if url not in merged:
                row = dict(hit)
                row["profile_url"] = url
                row["provider"] = "public_serp"
                row["backends"] = []
                merged[url] = row
                order.append(url)
            row = merged[url]
            if backend and backend not in row["backends"]:
                row["backends"].append(backend)
            for key in ("full_name", "title", "company_name", "country_code"):
                if not row.get(key) and hit.get(key):
                    row[key] = hit[key]
    return [merged[url] for url in order]


def _piece(outcome: BackendOutcome) -> str:
    label = _SHORT.get(outcome.name, outcome.label)
    if outcome.skip == "no_key":
        return f"{label}: no key"
    if outcome.skip == "quota":
        return f"{label}: quota"
    if outcome.skip == "request_limit":
        return f"{label}: skipped (request limit)"
    if outcome.skip == "robots":
        return f"{label}: skipped (robots.txt)"
    count = len(outcome.hits or [])
    if outcome.status == "ok":
        return f"{label}: {count} hits"
    if outcome.status == "quota":
        return f"{label}: quota ({count} hits)"
    if outcome.status == "blocked":
        return f"{label}: blocked ({count} hits)"
    return f"{label}: {outcome.status or 'skipped'}"


def _account(db: Session, outcome: BackendOutcome) -> None:
    if not outcome.called:
        return
    db.add(
        ProviderUsage(
            provider=outcome.name,
            operation="linkedin_search",
            success=outcome.status == "ok",
            http_status=outcome.http_status or None,
            scope="global",
            detail=(outcome.detail or "")[:500],
        )
    )


def _summary(query: str, outcomes: list[BackendOutcome], hit_count: int) -> str:
    parts = [_piece(item) for item in outcomes]
    text = f"Query {query}. " + ". ".join(part for part in parts if part)
    if hit_count == 0:
        text += ". Nothing was invented."
    return text


def run_linkedin_search(target, *, fetcher=None, db: Session | None = None, transport=None) -> tuple[list[dict], str]:
    """One query, every enabled engine, merged by profile URL."""
    if not getattr(target, "autonomous", False):
        return [], "This target is not set to run on its own. No request was sent."
    owns = False
    nested = None
    if db is None:
        from backend.database import SessionLocal

        db = SessionLocal()
        owns = True
    else:
        nested = db.begin_nested()
    try:
        query = next_query(target)
        target.last_query = query
        today = datetime.utcnow().date().isoformat()
        rows = ensure_search_backends(db)
        outcomes: list[BackendOutcome] = []
        calls: list[PreparedCall] = []
        for row in rows:
            if not row.enabled:
                continue
            creds = resolve_credentials(db, row.name)
            if not credentials_ready(row.name, creds):
                _remember(row, status="no_key", detail="no key", counted=False, today=today)
                outcomes.append(BackendOutcome(row.name, row.label, "no_key", "no key", [], False, 0, skip="no_key"))
                continue
            if _calls_today(row, today) >= int(row.daily_cap or 0):
                _remember(row, status="quota", detail="daily cap", counted=False, today=today)
                outcomes.append(BackendOutcome(row.name, row.label, "quota", "daily cap", [], False, 0, skip="quota"))
                continue
            if int(row.max_requests_per_run or 0) < 1:
                _remember(row, status=row.last_status or "", detail="request limit", counted=False, today=today)
                outcomes.append(
                    BackendOutcome(row.name, row.label, "", "request limit", [], False, 0, skip="request_limit")
                )
                continue
            calls.append(_request_for(row.name, query, creds))

        ran: dict[str, BackendOutcome] = {}
        if calls:
            workers = min(len(calls), 5)

            def _one(call: PreparedCall) -> BackendOutcome:
                return _run_call(call, query, transport=transport, fetcher=fetcher if call.name == "duckduckgo" else None)

            if workers == 1:
                ran[calls[0].name] = _one(calls[0])
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {call.name: pool.submit(_one, call) for call in calls}
                    for name, future in futures.items():
                        try:
                            ran[name] = future.result()
                        except Exception:
                            logger.warning("search backend %s failed", name)
                            ran[name] = BackendOutcome(name, _SHORT.get(name, name), "blocked", "request failed", [], True, 0)

        ordered: list[BackendOutcome] = []
        groups: list[tuple[str, list[dict]]] = []
        for row in rows:
            if not row.enabled:
                continue
            outcome = next((item for item in outcomes if item.name == row.name), None)
            if outcome is None:
                outcome = ran.get(row.name)
            if outcome is None:
                continue
            ordered.append(outcome)
            if outcome.called:
                detail = outcome.detail or f"{len(outcome.hits)} hits"
                _remember(row, status=outcome.status, detail=detail, counted=True, today=today)
                _account(db, outcome)
                groups.append((row.name, outcome.hits))
            elif outcome.skip == "robots" or outcome.status:
                _remember(row, status=outcome.status or "blocked", detail=outcome.detail, counted=False, today=today)
        hits = merge_profile_hits(groups)
        advance_cursor(target)
        summary = _summary(query, ordered, len(hits))
        if owns:
            db.commit()
        else:
            db.flush()
            if nested is not None:
                nested.commit()
        return hits, summary
    except Exception:
        logger.warning("linkedin search failed before a backend finished")
        if owns:
            db.rollback()
        elif nested is not None:
            nested.rollback()
        return [], "LinkedIn search did not finish. Nothing was invented."
    finally:
        if owns and db is not None:
            db.close()
