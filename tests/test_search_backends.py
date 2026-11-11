"""LinkedIn search engines: merge, isolation, skipped keys, and secret redaction."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from backend.models import (
    DiscoveryProviderSecret,
    DiscoveryRun,
    DiscoverySearchBackend,
    DiscoveryStageAttempt,
    DiscoveryTarget,
    GlobalEvidence,
    GlobalPerson,
    User,
)
from backend.services.crypto import decrypt_value
from backend.services.discovery_engine.pipeline import execute_run
from backend.services.discovery_engine.search_backends import (
    ensure_search_backends,
    merge_profile_hits,
    run_linkedin_search,
    update_search_backend,
)
from backend.services.discovery_engine.settings import ensure_settings
from backend.services.discovery_engine.spend import usage_report
from backend.services.discovery_engine.ssrf import SEARCH_API_HOSTS, decode_http_body


def _client():
    from backend.main import app

    return TestClient(app)


def _admin(client, db, email):
    created = client.post(
        "/api/auth/register",
        json={"email": email, "password": "test-password-123", "full_name": "Search Admin"},
    )
    assert created.status_code == 200, created.text
    row = db.query(User).filter_by(email=email).one()
    row.is_platform_admin = True
    db.commit()
    logged = client.post("/api/auth/admin/login", json={"email": email, "password": "test-password-123"})
    assert logged.status_code == 200, logged.text
    return {"Authorization": f"Bearer {logged.json()['access_token']}"}


def _target(db):
    target = DiscoveryTarget(
        name="Search engines",
        scheduling_status="active",
        autonomous=True,
        schedule_type="interval",
        interval_minutes=120,
        criteria={
            "geography": {"include_countries": ["GB"]},
            "titles": {"exact": ["java engineer"], "synonyms": [], "exclude": []},
        },
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def _enable(db, name, *, limit=5, daily=100):
    ensure_search_backends(db)
    row = db.get(DiscoverySearchBackend, name)
    row.enabled = True
    row.max_requests_per_run = limit
    row.daily_cap = daily
    db.commit()
    return row


_TITLE = "Ada Found - CTO - Example Solar | LinkedIn"
_SNIPPET = "Location: London, United Kingdom."
_LINK = "https://uk.linkedin.com/in/Ada-Found?trk=public"
_OTHER = "https://www.linkedin.com/in/bea-holm"


def _google_body():
    return json.dumps(
        {"items": [{"title": _TITLE, "snippet": _SNIPPET, "link": _LINK}]}
    ).encode()


def test_merge_dedups_normalized_linkedin_urls_and_keeps_every_backend():
    merged = merge_profile_hits(
        [
            (
                "brave",
                [{
                    "profile_url": "https://uk.linkedin.com/in/Ada-Found?trk=1",
                    "full_name": "Ada Found",
                    "title": "CTO",
                    "company_name": "",
                    "country_code": None,
                    "provider": "public_serp",
                }],
            ),
            (
                "google_cse",
                [{
                    "profile_url": "https://www.linkedin.com/in/ada-found/",
                    "full_name": "Ada Found",
                    "title": "",
                    "company_name": "Example Solar",
                    "country_code": "GB",
                    "provider": "public_serp",
                }],
            ),
            (
                "serper",
                [{
                    "profile_url": "https://www.linkedin.com/in/bea-holm",
                    "full_name": "Bea Holm",
                    "title": "Java Engineer",
                    "company_name": "Northwind",
                    "country_code": "GB",
                }],
            ),
        ]
    )
    assert len(merged) == 2
    ada = next(item for item in merged if item["full_name"] == "Ada Found")
    assert ada["profile_url"] == "https://www.linkedin.com/in/ada-found"
    assert ada["backends"] == ["brave", "google_cse"]
    assert ada["company_name"] == "Example Solar"
    assert ada["country_code"] == "GB"
    assert ada["title"] == "CTO"
    assert ada["provider"] == "public_serp"


def test_one_backend_failing_does_not_fail_the_run(db):
    _enable(db, "google_cse")
    _enable(db, "brave")
    update_search_backend(db, "google_cse", {"api_key": "google-key-fixture", "cx": "cx-fixture"})
    update_search_backend(db, "brave", {"api_key": "brave-key-fixture"})
    _enable(db, "duckduckgo")
    seen = []

    def transport(name, url, method, headers, body):
        seen.append(name)
        if name == "brave":
            raise RuntimeError("403 from the vendor")
        if name == "google_cse":
            return 200, _google_body()
        if "robots.txt" in url:
            return 200, b"User-agent: *\nAllow: /html\n"
        return 200, b""

    target = _target(db)
    hits, summary = run_linkedin_search(target, db=db, transport=transport)
    assert len(hits) == 1
    assert hits[0]["full_name"] == "Ada Found"
    assert hits[0]["country_code"] == "GB"
    assert hits[0]["company_name"] == "Example Solar"
    assert "Google CSE: 1 hits" in summary
    assert "Brave: blocked" in summary
    assert "google_cse" in seen
    assert "brave" in seen

    settings = ensure_settings(db)
    settings.enabled = True
    db.commit()
    run = DiscoveryRun(target_id=target.id, idempotency_key="search-backends-1", status="queued", scheduled_for=datetime.utcnow())
    db.add(run)
    db.commit()

    import backend.services.discovery_engine.search_backends as engines

    def fake_http(call, *, transport=None, fetcher=None):
        if call.name == "brave":
            return 403, b"forbidden", ""
        if call.name == "google_cse":
            return 200, json.dumps(
                {"items": [{"title": "Bea Holm - Java Engineer - Northwind | LinkedIn", "snippet": _SNIPPET, "link": _OTHER}]}
            ).encode(), ""
        if call.url.endswith("/robots.txt"):
            return 200, b"User-agent: *\nAllow: /html\n", ""
        return 200, b"", ""

    original = engines._http
    engines._http = fake_http
    try:
        execute_run(db, run)
    finally:
        engines._http = original
    db.refresh(run)
    assert run.status == "succeeded"
    detail = (
        db.query(DiscoveryStageAttempt)
        .filter(DiscoveryStageAttempt.run_id == run.id, DiscoveryStageAttempt.stage == "discover")
        .one()
    )
    assert detail.status == "ingested"
    assert "Brave: blocked" in detail.detail
    assert "Google CSE: 1 hits" in detail.detail
    assert "DuckDuckGo:" in detail.detail
    person = db.query(GlobalPerson).filter(GlobalPerson.full_name == "Bea Holm").one()
    providers = [
        row.provider
        for row in db.query(GlobalEvidence).filter(
            GlobalEvidence.subject_id == person.id,
            GlobalEvidence.field_name == "search_backend",
        )
    ]
    assert "google_cse" in providers


def test_missing_key_is_skipped_and_a_quota_or_limit_does_not_call(db, monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    _enable(db, "brave")
    _enable(db, "serper")
    update_search_backend(db, "serper", {"api_key": "serper-key-fixture"})
    row = db.get(DiscoverySearchBackend, "serper")
    row.calls_day = datetime.utcnow().date().isoformat()
    row.calls_today = row.daily_cap
    brave_limit = db.get(DiscoverySearchBackend, "google_cse")
    brave_limit.max_requests_per_run = 0
    brave_limit.enabled = True
    db.commit()
    update_search_backend(db, "google_cse", {"api_key": "google-key-fixture", "cx": "cx-fixture"})
    limited = db.get(DiscoverySearchBackend, "google_cse")
    limited.max_requests_per_run = 0
    db.commit()
    called = []

    def transport(name, url, method, headers, body):
        called.append(name)
        return 200, b"{}"

    target = _target(db)
    _hits, summary = run_linkedin_search(target, db=db, transport=transport)
    assert "brave" not in called
    assert "google_cse" not in called
    assert "serper" not in called
    assert "Brave: no key" in summary
    assert "Serper: quota" in summary
    assert "Google CSE: skipped (request limit)" in summary
    assert db.get(DiscoverySearchBackend, "brave").last_status == "no_key"


def test_saved_secrets_are_not_returned_by_the_api(db):
    secret = "gcs-test-key-do-not-leak-9f3a"
    cx = "cx-do-not-leak-11aa"
    with _client() as client:
        headers = _admin(client, db, "search-secret-admin@example.com")
        customer = client.post(
            "/api/auth/register",
            json={"email": "search-secret-user@example.com", "password": "test-password-123", "full_name": "User"},
        )
        user_headers = {"Authorization": f"Bearer {customer.json()['access_token']}"}
        assert client.get("/api/platform/discovery/search-backends", headers=user_headers).status_code == 401
        saved = client.patch(
            "/api/platform/discovery/search-backends/google_cse",
            headers=headers,
            json={"api_key": secret, "cx": cx, "enabled": False, "max_requests_per_run": 3, "daily_cap": 40},
        )
        assert saved.status_code == 200, saved.text
        body = saved.text
        assert secret not in body
        assert cx not in body
        item = next(row for row in saved.json()["items"] if row["name"] == "google_cse")
        assert item["enabled"] is True
        assert item["credential_configured"] is True
        assert item["cx_configured"] is True
        assert item["max_requests_per_run"] == 3
        assert item["daily_cap"] == 40
        assert "api_key" not in item
        assert "cx" not in item
        listed = client.get("/api/platform/discovery/search-backends", headers=headers)
        assert secret not in listed.text
        assert cx not in listed.text
    db.expire_all()
    stored = db.get(DiscoveryProviderSecret, "google_cse")
    assert secret not in stored.ciphertext
    plain = json.loads(decrypt_value(stored.ciphertext))
    assert plain["api_key"] == secret
    assert plain["cx"] == cx


def test_usage_counts_each_backend_and_calls_use_the_allowlist(db, monkeypatch):
    update_search_backend(db, "serper", {"api_key": "serper-allow-fixture"})
    update_search_backend(db, "serpapi", {"api_key": "serpapi-allow-fixture"})
    db.get(DiscoverySearchBackend, "duckduckgo").enabled = False
    db.commit()
    monkeypatch.delenv("MARK_DISABLE_CONTACT_NETWORK", raising=False)
    pinned = []

    def fake_pinned(url, addresses, timeout, max_bytes, **kwargs):
        pinned.append({"url": url, "method": kwargs.get("method"), "headers": kwargs.get("extra_headers") or {}, "body": kwargs.get("body")})
        host = urlparse(url).hostname
        if host == "google.serper.dev":
            raw = json.dumps({"organic": [{"title": _TITLE, "snippet": _SNIPPET, "link": _LINK}]}).encode()
        else:
            raw = json.dumps(
                {"organic_results": [{"title": "Bea Holm - Java Engineer - Northwind | LinkedIn", "snippet": _SNIPPET, "link": _OTHER}]}
            ).encode()
        return 200, None, raw, {}

    def fake_fetch(url, *, opener, allowed_hosts, **_kwargs):
        host = (urlparse(url).hostname or "").lower()
        assert host in allowed_hosts
        assert host in SEARCH_API_HOSTS
        _status, _location, body, *_rest = opener(url, ["203.0.113.10"], 1, 1000)
        return body

    monkeypatch.setattr("backend.services.discovery_engine.search_backends.pinned_opener", fake_pinned)
    monkeypatch.setattr("backend.services.discovery_engine.search_backends.safe_fetch", fake_fetch)
    target = _target(db)
    hits, summary = run_linkedin_search(target, db=db)
    assert {hit["full_name"] for hit in hits} == {"Ada Found", "Bea Holm"}
    assert "Serper: 1 hits" in summary
    assert "SerpAPI: 1 hits" in summary
    assert "serper-allow-fixture" not in summary
    assert "serpapi-allow-fixture" not in summary
    methods = {item["method"] for item in pinned}
    assert "POST" in methods
    serper = next(item for item in pinned if "serper.dev" in item["url"])
    assert serper["headers"]["X-API-KEY"] == "serper-allow-fixture"
    assert b"java engineer" in serper["body"].lower() or b"java" in serper["body"].lower()
    report = usage_report(db)
    assert report["providers"]["serper"] == 1
    assert report["providers"]["serpapi"] == 1
    assert report["requests"] >= 2


def test_environment_key_is_used_when_nothing_is_saved(db, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-env-fixture")
    monkeypatch.delenv("GOOGLE_CSE_API_KEY", raising=False)
    _enable(db, "brave")
    db.get(DiscoverySearchBackend, "duckduckgo").enabled = False
    db.commit()
    seen = []

    def transport(name, url, method, headers, body):
        seen.append((name, headers.get("X-Subscription-Token")))
        return 200, b'{"web":{"results":[]}}'

    _hits, summary = run_linkedin_search(_target(db), db=db, transport=transport)
    assert seen == [("brave", "brave-env-fixture")]
    assert "Brave: 0 hits" in summary
    assert "brave-env-fixture" not in summary
    from backend.services.discovery_engine.search_backends import search_backend_report

    report = search_backend_report(db)
    brave = next(item for item in report if item["name"] == "brave")
    assert brave["env_key"] is True
    assert brave["credential_configured"] is False
    assert "brave-env-fixture" not in json.dumps(report)


def test_chunked_http_bodies_decode():
    raw = b"5\r\nhello\r\n0\r\n\r\n"
    assert decode_http_body(raw, {"transfer-encoding": "chunked"}) == b"hello"
