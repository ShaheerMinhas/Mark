"""Pinned email-page fetches, robots skip counts, and opt-in discovery sources."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.models import DiscoveryRun, DiscoverySource, DiscoveryTarget, GlobalCompany, GlobalPerson
from backend.services.EmailModule.robots import clear_robots_cache, fetch_public_page, robots_skipped_count
from backend.services.EmailModule.web import _fetch_text
from backend.services.discovery_engine.ingest import ingest_hits
from backend.services.discovery_engine.sources import (
    collect_companies_house,
    collect_events,
    collect_extra_sources,
    collect_github,
    collect_google_places,
    ensure_sources,
    save_source_secret,
    update_source,
)
from backend.services.discovery_engine.spend import cache_health
from backend.services.discovery_engine.ssrf import SsrfError, pinned_opener


def _client():
    from backend.main import app

    return TestClient(app)


def _allow(url: str) -> bytes:
    if url.endswith("/robots.txt"):
        return b"User-agent: *\nAllow: /\n"
    return b""


def test_disallowed_pages_are_not_fetched_and_are_counted(db):
    clear_robots_cache()
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nDisallow: /private\n"
        return b"Ada Example ada.example@example.com"

    assert fetch_public_page("https://example.com/private", fetch=fetch) == ""
    assert "https://example.com/private" not in calls
    assert robots_skipped_count(db) == 1
    clear_robots_cache()
    calls.clear()

    def broken(url: str) -> bytes:
        calls.append(url)
        raise SsrfError("resolved address is not public")

    assert fetch_public_page("https://blocked.example/team", fetch=broken) == ""
    assert robots_skipped_count(db) == 1
    assert all(url.endswith("/robots.txt") for url in calls)


def test_email_page_fetch_uses_the_pinned_opener(monkeypatch):
    clear_robots_cache()
    monkeypatch.delenv("MARK_DISABLE_CONTACT_NETWORK", raising=False)
    calls = []

    def fake_fetch(url, *, opener, allowed_hosts, **_kwargs):
        calls.append((url, opener, set(allowed_hosts)))
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        return b"Ada Example, CTO"

    monkeypatch.setattr("backend.services.EmailModule.robots.safe_fetch", fake_fetch)
    text = _fetch_text("https://example.com/team")
    assert "Ada Example" in text
    assert calls
    assert all(opener is pinned_opener for _url, opener, _hosts in calls)
    assert {"example.com"} in [hosts for _url, _opener, hosts in calls]


def test_new_sources_default_off_and_linkedin_is_first(db):
    rows = ensure_sources(db)
    db.commit()
    linkedin = next(row for row in rows if row.name == "linkedin")
    assert linkedin.enabled is True
    assert linkedin.priority < min(row.priority for row in rows if row.name != "linkedin")
    assert all(row.enabled is False for row in rows if row.name != "linkedin")
    calls = []
    hits, note = collect_extra_sources(db, DiscoveryTarget(name="Quiet", titles=["cto"], countries=["SE"]), fetch=lambda url: calls.append(url))
    assert hits == []
    assert calls == []
    assert "No additional" in note


def test_company_site_extracts_a_published_person_and_dedups(db):
    clear_robots_cache()
    db.add(GlobalCompany(name="Example Co", domain="example.com", country_code="SE", name_normalized="example co"))
    db.commit()
    target = DiscoveryTarget(name="Sites", titles=["cto"], countries=["SE"])

    def fetch(url: str) -> bytes:
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        if url.endswith("/team"):
            return b"<p>Ada Example, CTO ada.example@example.com</p>"
        return b""

    hits, _note = collect_extra_sources(db, target, fetch=fetch)
    db.query(DiscoverySource).filter(DiscoverySource.name == "company_site").update({"enabled": True})
    db.commit()
    hits, _note = collect_extra_sources(db, target, fetch=fetch)
    assert [hit["full_name"] for hit in hits] == ["Ada Example"]
    assert hits[0]["email"] == "ada.example@example.com"
    assert hits[0]["email_status"] == "found"
    assert hits[0]["provider"] == "company_site"
    ingest_hits(db, hits)
    ingest_hits(db, hits)
    assert db.query(GlobalPerson).filter(GlobalPerson.full_name == "Ada Example").count() == 1


def test_github_keeps_only_published_emails_and_real_names(db):
    clear_robots_cache()

    def fetch(url: str) -> bytes:
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        if "search/users" in url:
            return json.dumps({"items": [{"login": "ada"}, {"login": "noname"}]}).encode()
        if url.endswith("/users/ada"):
            return json.dumps({"login": "ada", "name": "Ada Example", "email": "ada@example.com", "company": "Example"}).encode()
        if url.endswith("/users/noname"):
            return json.dumps({"login": "noname", "name": "Ada", "email": "hidden@example.com"}).encode()
        return b"{}"

    hits, _note = collect_github(db, DiscoveryTarget(name="Hub", titles=["cto"]), fetch=fetch, limit=6)
    assert [hit["full_name"] for hit in hits] == ["Ada Example"]
    assert hits[0]["email"] == "ada@example.com"
    clear_robots_cache()

    def limited(url: str) -> bytes:
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        return b'{"message": "API rate limit exceeded"}'

    hits, note = collect_github(db, DiscoveryTarget(name="Hub", titles=["cto"]), fetch=limited, limit=6)
    assert hits == []
    assert "rate limit" in note


def test_keyed_sources_do_not_call_without_prerequisites(db):
    clear_robots_cache()

    def boom(url: str) -> bytes:
        raise AssertionError(url)

    target = DiscoveryTarget(name="Keys", titles=["director"])
    _hits, note = collect_companies_house(db, target, fetch=boom, limit=3)
    assert "No request was sent" in note
    save_source_secret(db, "companies_house", "fixture-key")

    def officers(url: str) -> bytes:
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        return json.dumps({"items": [{"title": "Ada Example"}]}).encode()

    hits, _note = collect_companies_house(db, target, fetch=officers, limit=4)
    assert hits[0]["full_name"] == "Ada Example"
    assert hits[0]["email"] is None
    _hits, note = collect_extra_sources(db, target, fetch=boom)
    from backend.services.discovery_engine.sources import collect_sec

    _hits, note = collect_sec(db, db.get(DiscoverySource, "sec_edgar"), target, fetch=boom, limit=3)
    assert "No request was sent" in note
    update_source(db, "sec_edgar", {"sec_contact_email": "operator@example.com"})

    def filings(url: str) -> bytes:
        if url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        return json.dumps({"hits": {"hits": [{"_source": {"display_names": ["Example, Ada (CTO)"]}}]}}).encode()

    hits, _note = collect_sec(db, db.get(DiscoverySource, "sec_edgar"), target, fetch=filings, limit=4)
    assert hits[0]["full_name"] == "Ada Example"
    assert hits[0]["email"] is None
    _hits, note = collect_google_places(db, db.get(DiscoverySource, "google_places"), target, fetch=boom, limit=3)
    assert "No request was sent" in note


def test_events_and_directories_honor_robots_and_approval(db):
    clear_robots_cache()
    before = robots_skipped_count(db)

    def fetch(url: str) -> bytes:
        if "duckduckgo.com" in url and url.endswith("/robots.txt"):
            return b"User-agent: *\nAllow: /\n"
        if "duckduckgo.com/html" in url:
            return b'<a href="https://conf.example/speakers">Speakers</a>'
        if "conf.example" in url and url.endswith("/robots.txt"):
            return b"User-agent: *\nDisallow: /\n"
        raise AssertionError(url)

    hits, _note = collect_events(DiscoveryTarget(name="Talks", titles=["cto"]), fetch=fetch, limit=8)
    assert hits == []
    assert robots_skipped_count(db) == before + 1
    calls = []
    from backend.services.discovery_engine.sources import collect_directories

    hits, note = collect_directories(db.get(DiscoverySource, "directories") or ensure_sources(db)[-1], fetch=lambda url: calls.append(url), limit=4)
    row = db.get(DiscoverySource, "directories")
    hits, note = collect_directories(row, fetch=lambda url: calls.append(url), limit=4)
    assert hits == []
    assert calls == []
    assert "No directory host" in note
    try:
        update_source(db, "directories", {"approved_hosts": ["zoominfo.com"]})
        assert False
    except ValueError:
        pass


def test_customer_cannot_enable_a_source(db):
    with _client() as client:
        created = client.post(
            "/api/auth/register",
            json={"email": "source-customer@example.com", "password": "test-password-123", "full_name": "Customer"},
        )
        headers = {"Authorization": f"Bearer {created.json()['access_token']}"}
        assert client.patch("/api/platform/discovery/sources/github", json={"enabled": True}, headers=headers).status_code == 401


def test_cache_health_reports_robots_skips_and_source_yield(db):
    health = cache_health(db)
    assert "robots_skipped_pages" in health
    names = [item["name"] for item in health["sources"]]
    assert names[0] == "linkedin"
    assert "with_email" in health["sources"][0]
    assert "with_phone" in health["sources"][0]
